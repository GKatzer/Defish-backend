# utils/recommendations.py
"""Care recommendations for a fish, depending on the diagnosis.

The keys of RECOMMENDATIONS correspond to the classes returned by the ML model
(worker.py: det["class"] / det["classification_class"]):
dermatomycosis, fin_rot, healthy, hexamitosis,
mycobacteriosis, oodiniosis, plistophorosis.
(many_fish and not_a_fish are filtered out on the ML side and never reach the results)
"""

DEFAULT_RECOMMENDATION = "The diagnosis was not recognised. It is recommended to consult a fish pathologist and to keep observing the fish."

NO_FISH_RECOMMENDATION = "No fish was found in the photo. Take a sharper picture in good light so that the whole fish is visible."

RECOMMENDATIONS = {
    "healthy": "The fish is healthy. Keep up the optimal conditions: temperature 24-26°C, pH 6.5-7.5, regular water changes.",
    "fin_rot": "Fin rot. Recommendations: improve the water quality, use antibacterial medication (antibiotics), raise the temperature.",
    "dermatomycosis": "Dermatomycosis. Recommendations: antifungal medication (antifungal baths based on malachite green or formalin), isolation of the sick fish, better water quality and aquarium hygiene.",
    "mycobacteriosis": "Mycobacteriosis (fish tuberculosis). A chronic disease that is hard to cure. Recommendations: isolate the sick fish; if the damage is severe, euthanise it to avoid infecting the others; thoroughly disinfect the aquarium and equipment. The pathogen is dangerous to humans: wear gloves.",
    "oodiniosis": "Oodiniosis (velvet disease). Recommendations: darken the aquarium (the parasite lives on photosynthesis), raise the temperature by 2-3°C, give salt baths, use specialised antiparasitic medication (copper-based), quarantine.",
    "hexamitosis": "Hexamitosis (\"hole-in-the-head\" disease). Recommendations: metronidazole-based medication, better water quality and diet (vitamin-enriched food), quarantine of the sick fish.",
    "plistophorosis": "Plistophorosis (neon tetra disease). There is no effective treatment. Recommendations: immediately isolate or euthanise the sick fish to avoid infecting the others, disinfect the aquarium and equipment.",
    # Reserve entries: they are not classes of the current model,
    # kept in case of a manual or old diagnosis text.
    "ich": "Ichthyophthiriasis (ich, white spot disease). Recommendations: raise the temperature to 28-30°C, add salt 1-3 g/l, use malachite green.",
    "fungus": "Fungal infection. Recommendations: antifungal medication, better aquarium hygiene, salt baths.",
}

# Reserve matching by keywords, for diagnoses that do not match the model's
# classes exactly (for example free-form or composite text).
_KEYWORD_FALLBACKS = [
    (("ichthyo", "semolina", "white spot"), "ich"),
    (("rot", "fin"), "fin_rot"),
    (("dermatomyc",), "dermatomycosis"),
    (("fungus", "fungal"), "fungus"),
    (("mycobacter",), "mycobacteriosis"),
    (("oodin", "velvet"), "oodiniosis"),
    (("hexamit",), "hexamitosis"),
    (("plistophor", "pleistophor"), "plistophorosis"),
    (("healthy",), "healthy"),
]


def get_recommendations(diagnosis: str) -> str:
    diagnosis_normalized = diagnosis.strip().lower()

    # 1. Exact match with a model class (the main path)
    if diagnosis_normalized in RECOMMENDATIONS:
        return RECOMMENDATIONS[diagnosis_normalized]

    # 2. Matching by keywords (composite or free-form diagnoses)
    for keywords, key in _KEYWORD_FALLBACKS:
        if any(kw in diagnosis_normalized for kw in keywords):
            return RECOMMENDATIONS[key]

    return DEFAULT_RECOMMENDATION


def summarize_recommendations(classes: list[str]) -> str:
    """A common text for the whole photo: one piece of advice for each class that occurs.

    The classes are taken in order of appearance, without repeats. The advice for healthy is added
    only if there are no sick fish in the photo. For an empty list (no fish found)
    NO_FISH_RECOMMENDATION is returned, not the advice "the fish is healthy".
    """
    if not classes:
        return NO_FISH_RECOMMENDATION

    unique = list(dict.fromkeys(c.strip().lower() for c in classes))
    chosen = [c for c in unique if c != "healthy"] or unique
    # identical texts (for example, several unrecognised classes) are collapsed
    return "\n\n".join(dict.fromkeys(get_recommendations(c) for c in chosen))
