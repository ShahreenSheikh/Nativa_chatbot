"""Production entrypoint.
Loads the existing FastAPI app, then installs the NativaCare runtime wrapper.
Use `uvicorn app:app` (Procfile updated accordingly).
"""
import main as _main
import ai as _ai
from chat_runtime import get_ai_response as _safe_get_ai_response

# main.py's route functions resolve this module global at request time, so
# replacing it here covers both website /chat and WhatsApp webhook replies.
_main.get_ai_response = _safe_get_ai_response

# Current service language policy: Urdu removed; Arabic explicitly subject
# to availability. Keep canonical "Arabic" internally for midwife matching.
_ai.SUPPORTED_LANGUAGES = ["English", "Arabic"]
def _language_prompt(service_name=""):
    prefix=f"Great — {service_name} it is. " if service_name else ""
    return (prefix + "In which language would you like your session?\n\n"
            "  • English\n  • Arabic (upon availability)\n\n"
            "Reply with 'English' or 'Arabic'.")
_ai.render_language_prompt = _language_prompt

app = _main.app
