"""
app/services/face.py — Face recognition (dlib/face_recognition).
Degrades gracefully to bypass mode if not installed.
"""
import base64
import io
import json

FACE_READY = False
try:
    import face_recognition
    import numpy as np
    from PIL import Image
    FACE_READY = True
    print("✅ face_recognition ready — biometric verification active")
except ImportError:
    print("⚠️  face_recognition not installed — running in BYPASS mode (face always passes)")


def _clean_b64(b64: str) -> str:
    return b64.split(",")[1] if "," in b64 else b64


def extract_embedding(b64_image: str):
    """Extract a 128-d face embedding from a base64 JPEG. Returns list or None."""
    if not FACE_READY:
        return None
    try:
        img_bytes = base64.b64decode(_clean_b64(b64_image))
        pil_img   = Image.open(io.BytesIO(img_bytes))
        pil_img.load()
        pil_img   = pil_img.convert("RGB")
        img_array = np.array(pil_img, dtype=np.uint8)
        img_array = np.ascontiguousarray(img_array).copy()

        try:
            locations = face_recognition.face_locations(img_array, model="hog")
        except Exception as e:
            print(f"[FACE] face_locations failed: {e}", flush=True)
            return None
        if not locations:
            return None
        try:
            encodings = face_recognition.face_encodings(
                img_array, known_face_locations=locations, model="small")
        except Exception as e:
            print(f"[FACE] face_encodings failed: {e}", flush=True)
            return None
        return encodings[0].tolist() if encodings else None
    except Exception as e:
        print(f"[FACE] embedding error: {e}", flush=True)
        return None


def compare_faces(stored_embedding: list, live_b64: str) -> dict:
    """Compare stored embedding against a live camera frame."""
    if not FACE_READY:
        return {"match": True, "confidence": 0.5,
                "reason": "Bypass — face_recognition not installed", "bypass": True}
    live = extract_embedding(live_b64)
    if live is None:
        return {"match": False, "confidence": 0.0,
                "reason": "No face detected. Check lighting and face the camera.", "bypass": False}
    distance = float(
        face_recognition.face_distance([np.array(stored_embedding)], np.array(live))[0]
    )
    conf = round(max(0.0, 1.0 - distance), 3)
    return {
        "match":      distance < 0.45,
        "confidence": conf,
        "distance":   round(distance, 4),
        "reason":     "✅ Face matched" if distance < 0.45
                      else f"❌ Face too different (distance {distance:.3f})",
        "bypass":     False,
    }
