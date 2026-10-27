"""Speech recognition is told the names a caller is likely to say, most likely first."""

from app.voice.keyterms import MAX_TERMS, ClinicDirectory


def test_the_callers_own_name_comes_first_and_in_both_scripts() -> None:
    directory = ClinicDirectory(
        "Sunrise Clinic",
        providers=[{"id": "1", "name": f"Dr. Doctor {n}"} for n in range(120)],
        callers=["निमित देसाई"],
        expected=["Kit Balsar"],
    )
    terms = directory.keyterms
    assert terms[:3] == ("Sunrise Clinic", "निमित देसाई", "Nimit Desai")
    # The list is cut at the limit; patients expected soon are the first to go.
    assert len(terms) == MAX_TERMS and "Kit Balsar" not in terms


def test_patients_expected_soon_are_hinted_when_there_is_room() -> None:
    terms = ClinicDirectory("Sunrise Clinic", expected=["Kit Balsar", "Kit Balsar"]).keyterms
    assert terms == ("Sunrise Clinic", "Kit Balsar")
