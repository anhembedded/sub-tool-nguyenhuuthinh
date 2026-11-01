"""Editorial name decisions from an explicitly identified OCR name token."""
def resolve(expected_english,source_aliases,observed_name=None,confidence=0,ocr_english=None):
    if not observed_name or confidence<.8:
        return dict(decision='UNREADABLE_KEEP',english=expected_english,changed=False)
    if observed_name in source_aliases:
        return dict(decision='MATCH_KEEP',english=expected_english,changed=False)
    if not ocr_english or not ocr_english.strip():
        raise ValueError('Readable OCR mismatch requires contextual English rendering of OCR name; cannot call it unreadable')
    return dict(decision='MISMATCH_USE_OCR',english=ocr_english,changed=ocr_english!=expected_english,source_name=observed_name)
