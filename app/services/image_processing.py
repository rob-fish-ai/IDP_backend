import cv2
import numpy as np
from PIL import Image


def preprocess_for_ocr(
    pil_image: Image.Image,
    max_width: int = 1280,
    max_height: int = 1920,
) -> Image.Image:
    """Pre-process an image to maximize OCR accuracy.

    Pipeline:
    1. Grayscale
    2. Resize to target dimensions (aspect-ratio preserved)
    3. Background normalization (remove shadows/vignetting)
    4. CLAHE contrast enhancement
    5. Bilateral denoise (edge-preserving, fast)
    6. Sharpen to reinforce text edges
    7. Deskew
    8. White border padding to exact canvas size
    """
    img = np.array(pil_image)

    # 1. Grayscale
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        gray = img

    # 2. Resize first so every downstream filter runs on the small image.
    #    OCR target is 1280×1920; running CLAHE/denoise/deskew at full
    #    render resolution wastes ~5–10× the compute for no quality gain.
    h, w = gray.shape
    scale = min(max_width / w, max_height / h)
    if scale != 1.0:
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
        resized = cv2.resize(gray, (new_w, new_h), interpolation=interp)
    else:
        resized = gray
        new_w, new_h = w, h

    # 3. Background normalization. Kernel sized for the resized image.
    background = cv2.GaussianBlur(resized, (0, 0), sigmaX=31)
    normalized = cv2.divide(resized, background, scale=255)

    # 4. CLAHE — locally adaptive contrast enhancement
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(normalized)

    # 5. Bilateral filter — edge-preserving denoise. An order of magnitude
    #    faster than fastNlMeansDenoising and keeps text strokes crisp.
    denoised = cv2.bilateralFilter(enhanced, d=5, sigmaColor=35, sigmaSpace=5)

    # 6. Sharpen to reinforce text edges
    blurred = cv2.GaussianBlur(denoised, (0, 0), sigmaX=1.0)
    sharpened = cv2.addWeighted(denoised, 1.5, blurred, -0.5, 0)

    # 7. Deskew on the resized image — minAreaRect is much cheaper here.
    result = _deskew(sharpened)

    # 8. Pad to exact canvas size with white borders (centered).
    pad_w = max_width - new_w
    pad_h = max_height - new_h
    top = pad_h // 2
    bottom = pad_h - top
    left = pad_w // 2
    right = pad_w - left
    result = cv2.copyMakeBorder(
        result, top, bottom, left, right,
        cv2.BORDER_CONSTANT, value=255,
    )

    return Image.fromarray(result)


def _deskew(image: np.ndarray) -> np.ndarray:
    """Correct small skew angles using minimum-area bounding rectangle."""
    coords = np.column_stack(np.where(image < 128))
    if len(coords) < 50:
        return image

    angle = cv2.minAreaRect(coords)[-1]

    # cv2.minAreaRect returns angles in [-90, 0); normalise to [-45, 45]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    # Only correct small skews
    if abs(angle) > 10 or abs(angle) < 0.3:
        return image

    h, w = image.shape
    center = (w // 2, h // 2)
    rotation_matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        image, rotation_matrix, (w, h), flags=cv2.INTER_CUBIC, borderValue=255,
    )
    return rotated


# Content-loss detection thresholds, calibrated on a real half-lost page:
# a dense form page that OCR'd completely yielded ~0.014 chars per ink
# pixel; the page whose top half was silently dropped yielded ~0.003.
_CONTENT_LOSS_MIN_INK_RATIO = 0.03
_CONTENT_LOSS_CHARS_PER_INK = 0.006


def suspected_content_loss(image_path: str, text_len: int) -> bool:
    """True when a page's ink volume implies far more text than OCR returned.

    Catches the failure mode where OCR returns clean-looking text for PART
    of a dense form page and silently drops the rest — the quality
    composite stays above the vision threshold because the text it did
    return is coherent, so nothing else flags the page. Only fires on
    clearly dense pages (ink ratio above _CONTENT_LOSS_MIN_INK_RATIO);
    sparse letters and near-blank pages are never suspected.
    """
    try:
        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img is None or img.size == 0:
            return False
        ink_px = int((img < 128).sum())
        if ink_px / img.size < _CONTENT_LOSS_MIN_INK_RATIO:
            return False
        return text_len < ink_px * _CONTENT_LOSS_CHARS_PER_INK
    except Exception:
        return False
