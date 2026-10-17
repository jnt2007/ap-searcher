from ap_searcher import amenities as A


def states(text, params=None):
    return {k: v["state"] for k, v in A.detect(text, params).items()}


def test_structured_parking_beats_text():
    result = A.detect("Byt v klidné ulici.", {"garage": True})
    assert result["parking"]["state"] == A.YES
    assert "garage" in result["parking"]["evidence"]


def test_parking_lots_flag():
    assert A.detect("", {"parkingLots": True})["parking"]["state"] == A.YES


def test_false_structured_flags_are_not_evidence():
    result = A.detect("Nabízíme byt.", {"garage": False, "parkingLots": False})
    assert result["parking"]["state"] == A.UNKNOWN


def test_text_matches_are_maybe_not_yes():
    result = A.detect("Kuchyň s myčkou nádobí a indukční deskou, koupelna s vanou.")
    assert result["dishwasher"]["state"] == A.MAYBE
    assert result["induction"]["state"] == A.MAYBE
    assert result["bathtub"]["state"] == A.MAYBE
    assert "myčk" in result["dishwasher"]["evidence"]


def test_diacritics_free_ads_still_match():
    assert states("Kuchyne s myckou, koupelna s vanou, garazove stani.") == {
        "parking": A.MAYBE, "dishwasher": A.MAYBE, "induction": A.UNKNOWN,
        "bathtub": A.MAYBE, "cellar": A.UNKNOWN,
    }


def test_negations_suppress_matches():
    assert states("Byt bez myčky a bez parkování.")["dishwasher"] == A.UNKNOWN
    assert states("Byt bez myčky a bez parkování.")["parking"] == A.UNKNOWN
    assert states("V bytě není vana, pouze sprchový kout.")["bathtub"] == A.UNKNOWN


def test_street_names_do_not_fake_parking():
    # "náměstí Povstání" used to match a bare "stání" pattern
    assert states("Blízko náměstí Povstání, 5 minut pěšky.")["parking"] == A.UNKNOWN


def test_shower_alone_is_not_a_bathtub():
    assert states("Koupelna se sprchovým koutem a pračkou.")["bathtub"] == A.UNKNOWN


def test_person_name_is_not_a_bathtub():
    assert states("Kontakt: Ivana Nová, makléřka.")["bathtub"] == A.UNKNOWN


def test_parking_phrases():
    for text in ("Možnost parkování v ulici.", "K bytu náleží garážové stání.",
                 "Venkovní stání před domem.", "Garáž v suterénu."):
        assert states(text)["parking"] == A.MAYBE, text


def test_unknown_when_nothing_said():
    assert states("Světlý byt po rekonstrukci.") == {
        "parking": A.UNKNOWN, "dishwasher": A.UNKNOWN, "induction": A.UNKNOWN,
        "bathtub": A.UNKNOWN, "cellar": A.UNKNOWN,
    }


def test_fold_preserves_offsets():
    text = "Myčka, vana, garáž"
    assert len(A.fold(text)) == len(text)
    assert A.fold(text) == "mycka, vana, garaz"


def test_structured_cellar_is_yes():
    result = A.detect("Byt v cihlovém domě.", {"cellar": True})
    assert result["cellar"]["state"] == A.YES
    assert "cellar" in result["cellar"]["evidence"]


def test_cellar_area_alone_counts():
    assert A.detect("", {"cellar": False, "cellarArea": 4})["cellar"]["state"] == A.YES


def test_cellar_false_falls_through_to_text():
    # sreality's checkbox is often left unticked while the prose says otherwise; `maybe`
    # is the honest verdict, the same treatment a falsy `garage` gets
    result = A.detect("K bytu patří sklepní kóje.", {"cellar": False, "cellarArea": None})
    assert result["cellar"]["state"] == A.MAYBE
    assert "sklep" in result["cellar"]["evidence"]


def test_cellar_phrases():
    for text in ("K bytu náleží sklep.", "Součástí je sklepní kóje 3 m².",
                 "Ve sklepě je prádelna.", "Byt se sklepem.", "Vlastni sklipek pro vino."):
        assert states(text)["cellar"] == A.MAYBE, text


def test_cellar_negation():
    assert states("Byt je bez sklepa.")["cellar"] == A.UNKNOWN
    assert states("Ke bytu není sklep.")["cellar"] == A.UNKNOWN


def test_pantry_is_not_a_cellar():
    assert states("Vstupní hala s komorou a vestavěnou skříní.")["cellar"] == A.UNKNOWN


def test_spec_table_negation_after_the_label():
    # agents paste "Sklep: Není" / "Myčka: Není" tables into the description
    assert states("Elektřina: Převod na nájemce Sklep: Není")["cellar"] == A.UNKNOWN
    assert states("Myčka: Ne, Pračka: Ano")["dishwasher"] == A.UNKNOWN
    assert states("Sklep – není součástí.")["cellar"] == A.UNKNOWN


def test_trailing_negation_needs_the_label_separator():
    # free prose runs the other way: this ad is advertising parking, not denying it
    assert states("Parkování není problém, v ulici je vždy místo.")["parking"] == A.MAYBE


def test_nenalezi_negates():
    assert states("K bytu nenáleží sklep.")["cellar"] == A.UNKNOWN
    assert states("K bytu nenáleží žádný sklep.")["cellar"] == A.UNKNOWN
    assert states("Ačkoliv k bytu nenáleží garážové stání, lze zaparkovat.")["parking"] == A.UNKNOWN


def test_nejde_is_not_a_negation():
    # "it is not merely a parking space, but a real garage" - the parking is real
    assert states("Nejde pouze o vyhrazené parkovací stání, ale o garáž.")["parking"] == A.MAYBE
