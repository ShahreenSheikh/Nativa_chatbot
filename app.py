"""Production entrypoint and NativaCare policy overrides."""
import main as _main
import ai as _ai
import database as _catalog
from chat_runtime import get_ai_response as _safe_get_ai_response
from service_intents import SERVICE_KEYWORDS

# Website + WhatsApp use the same safeguarded brain.
_main.get_ai_response=_safe_get_ai_response

# No physical office: remove stale address/contact data from the catalog slice
# supplied to the LLM, and normalize legacy clinic location rows to home.
_original_info=_ai._db_get_clinic_info
async def _safe_info(*a,**k):
    info=dict(await _original_info(*a,**k));info.pop("address",None);info.pop("clinic_address",None);info["phone"]="971 50 7297197";return info
_ai._db_get_clinic_info=_safe_info
_original_normalize=_catalog._normalize_location_type
def _no_clinic(raw):
    val=_original_normalize(raw)
    if val=="online":return "online"
    if val=="clinic_or_online":return "online"
    if val in ("clinic","clinic_or_home"):return "home"
    return val
_catalog._normalize_location_type=_no_clinic

# Merge flyer vocabulary into live Google-Sheet service rows. This supplements,
# rather than replaces, any keywords staff maintain in the sheet.
_original_services=_ai._db_get_services
async def _enriched_services(*a,**k):
    rows=await _original_services(*a,**k)
    for s in rows:
        hay=" ".join(str(s.get(x,"") or "") for x in ("service_name","family_name","category")).lower();key=None
        if "nanny" in hay or "caregiver" in hay:key="nanny_training"
        elif "breastfeed" in hay or "lactation" in hay:key="breastfeeding_support"
        elif "postnatal" in hay or "postpartum" in hay or "recovery" in hay:key="postnatal_support"
        elif "antenatal" in hay or "prenatal" in hay or "pregnancy" in hay:key="antenatal_preparation"
        if key:
            existing=(s.get("keywords") or "").strip();extra=", ".join(SERVICE_KEYWORDS[key]);s["keywords"]=(existing+", "+extra).strip(", ")[:4000]
    return rows
_ai._db_get_services=_enriched_services

# Urdu removed. Arabic shown explicitly as subject to availability.
_ai.SUPPORTED_LANGUAGES=["English","Arabic"]
def _language_prompt(service_name=""):
    prefix=f"Great — {service_name} it is. " if service_name else ""
    return prefix+"In which language would you like your session?\n\n  • English\n  • Arabic (upon availability)\n\nReply with 'English' or 'Arabic'."
_ai.render_language_prompt=_language_prompt

app=_main.app
