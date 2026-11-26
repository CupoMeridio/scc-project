"""Costanti e classificazione clinica condivise tra training, ML API e SaaS backend."""

CLASS_BINS = [0.0, 3.0, 3.9, 5.0, 7.8, 10.0, 13.9, float("inf")]


def classify_glucose(glucose_mmol: float) -> int:
    """Mappa la glicemia continua (mmol/L) nella classe clinica discreta (1..7)."""
    for i in range(len(CLASS_BINS) - 1):
        if CLASS_BINS[i] <= glucose_mmol < CLASS_BINS[i + 1]:
            return i + 1
    return 7
