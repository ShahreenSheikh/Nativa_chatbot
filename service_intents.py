"""NativaCare service-intent vocabulary from the current service flyers.
Used as a deterministic supplement to the LLM so natural problem statements
and follow-up pronouns keep the correct service context.
"""
SERVICE_KEYWORDS = {
 "nanny_training": ["nanny","nanny training","caregiver","caregiver training","train my nanny","teach my nanny","newborn care training","newborn handling","safe handling","burping","swaddling","soothing","settling baby","baby bathing","newborn bathing","diapering","nappy","newborn cues","baby cues","growth milestones","development milestones","newborn first aid","baby first aid","travelling with baby","baby hygiene"],
 "breastfeeding_support": ["breastfeeding","breast feeding","breastfeed","lactation","latch","latching","poor latch","won't latch","wont latch","feeding hurts","painful feeding","painful breastfeeding","sore nipples","cracked nipples","nipple pain","milk supply","low milk supply","oversupply","engorgement","mastitis","blocked ducts","hand expression","express milk","pumping","breast pump","milk storage","cluster feeding","feeding cues","breast milk"],
 "postnatal_support": ["postnatal","postpartum","after birth","after delivery","after giving birth","new mother","new mum","new mom","postnatal recovery","postpartum recovery","recover from birth","postnatal bleeding","postpartum bleeding","lochia","perineal tear","episiotomy","stitches","c-section","c section","cesarean","caesarean","wound care","pelvic floor","back pain","pain after birth","fatigue","postpartum anxiety","postnatal anxiety","low mood","emotional wellbeing","feeling overwhelmed","struggling after birth"],
 "antenatal_preparation": ["antenatal","prenatal","pregnancy","pregnant","expecting","expecting baby","antenatal classes","prenatal classes","birth preparation","prepare for birth","prepare for delivery","childbirth","labour","labor","contractions","braxton hicks","stages of labour","waters breaking","birth plan","birth preferences","when to go hospital","pain relief","breathing techniques","birth positions","birthing positions","birth ball","counterpressure","partner support","birth partner","breastfeeding preparation","prepare for breastfeeding","colostrum","colostrum harvesting"]
}
# "newborn" deliberately is NOT a blind nanny-training keyword: breastfeeding
# and postnatal conversations often mention a newborn too. It only becomes a
# nanny-training signal when caregiver/training vocabulary is present.
EMPATHY_SIGNALS = ["struggling","struggle","having trouble","having problems","problem with","problems with","issue with","issues with","hard time","worried","worry","anxious","anxiety","overwhelmed","painful","hurts","hurt","pain","scared","difficult","difficulty","exhausted","can't cope","cant cope","not coping","upset","won't latch","wont latch","poor latch","not latching","low milk supply","sore nipples","cracked nipples","engorgement","mastitis","blocked ducts","bleeding","tear","episiotomy","stitches","c-section pain","c section pain","low mood","fatigue"]
FOLLOWUP_PRONOUNS = ["it","this","that","this service","that service","tell me more","more about it","how much is it","price of it","cost of it","book it","book this","availability for it"]

def detect_service_key(message: str):
    m=(message or "").lower()
    scores={k:sum(2 if " " in kw else 1 for kw in kws if kw in m) for k,kws in SERVICE_KEYWORDS.items()}
    if "newborn" in m and any(x in m for x in ("nanny","caregiver","training","train","teach","helper")):
        scores["nanny_training"] += 4
    best=max(scores,key=scores.get) if scores else None
    return best if best and scores[best]>0 else None

def is_context_followup(message: str):
    m=(message or "").lower().strip()
    return any(p in m for p in FOLLOWUP_PRONOUNS)

def needs_empathy(message: str):
    """True when the patient is describing a difficulty, symptom or concern.

    This intentionally keys off problem-language as well as emotional words:
    patients often say 'having problems with latching' without saying they are
    worried or in pain. Those messages still need acknowledgement before the
    bot presents services or prices.
    """
    m=(message or "").lower()
    return any(x in m for x in EMPATHY_SIGNALS)
