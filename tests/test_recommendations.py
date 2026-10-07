from utils.recommendations import (
    DEFAULT_RECOMMENDATION,
    NO_FISH_RECOMMENDATION,
    RECOMMENDATIONS,
    get_recommendations,
    summarize_recommendations,
)


def test_exact_class_is_case_and_whitespace_insensitive():
    assert get_recommendations("  Fin_Rot ") == RECOMMENDATIONS["fin_rot"]


def test_every_model_class_has_its_own_text():
    classes = ["healthy", "fin_rot", "dermatomycosis", "hexamitosis", "mycobacteriosis", "oodiniosis", "plistophorosis"]
    texts = {get_recommendations(c) for c in classes}
    assert len(texts) == len(classes)
    assert DEFAULT_RECOMMENDATION not in texts


def test_keyword_fallback_for_free_text():
    assert get_recommendations("Fin rot") == RECOMMENDATIONS["fin_rot"]


def test_unknown_diagnosis_falls_back_to_a_specialist():
    assert get_recommendations("something else") == DEFAULT_RECOMMENDATION


def test_summary_without_fish_is_not_the_healthy_text():
    assert summarize_recommendations([]) == NO_FISH_RECOMMENDATION
    assert NO_FISH_RECOMMENDATION != RECOMMENDATIONS["healthy"]


def test_summary_all_healthy_gives_the_healthy_text_once():
    assert summarize_recommendations(["healthy", "healthy"]) == RECOMMENDATIONS["healthy"]


def test_summary_drops_healthy_when_a_fish_is_sick():
    assert summarize_recommendations(["healthy", "fin_rot", "healthy"]) == RECOMMENDATIONS["fin_rot"]


def test_summary_keeps_every_disease_in_order_of_appearance():
    text = summarize_recommendations(["oodiniosis", "fin_rot", "oodiniosis"])
    assert text == RECOMMENDATIONS["oodiniosis"] + "\n\n" + RECOMMENDATIONS["fin_rot"]


def test_summary_collapses_unrecognised_classes():
    assert summarize_recommendations(["x", "y"]) == DEFAULT_RECOMMENDATION
