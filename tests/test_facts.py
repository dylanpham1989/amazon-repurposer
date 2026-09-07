from app.services.facts import check, facts_hint, normalize_numbers, significant_numbers


def test_thousand_separator_equivalence():
    assert normalize_numbers("1,299.50") == normalize_numbers("1299.5")
    assert normalize_numbers("1.299,50") == normalize_numbers("1299.5")   # châu Âu
    assert normalize_numbers("1,299") == normalize_numbers("1299")


def test_trivial_numbers_ignored():
    assert "2" not in significant_numbers("has 2 ports")
    assert "40" in significant_numbers("40mm driver")


def test_pass_on_faithful_paraphrase():
    o = "Sony WH-1000XM4, 30 hours battery, 40mm driver, weighs 254 g"
    n = "Sony WH-1000XM4 with 30 hours of battery, 40mm drivers, 254 g"
    assert check(o, n, "Sony") == []


def test_catches_invented_number():
    o = "30 hours battery, 40mm driver, 254 g"
    errs = check(o, "50 hours battery, 40mm driver, 254 g")
    assert any("bịa số" in e for e in errs)


def test_catches_dropped_number():
    o = "30 hours battery, 40mm driver, 254 g"
    errs = check(o, "long battery life with 40mm driver")
    assert any("mất số" in e for e in errs)


def test_catches_dropped_brand():
    o = "Sony WH-1000XM4, 30 hours battery"
    errs = check(o, "WH-1000XM4, 30 hours battery", "Sony")
    assert any("thương hiệu" in e for e in errs)


def test_catches_unchanged_output():
    o = "Sony WH-1000XM4, 30 hours battery"
    assert any("không đổi" in e for e in check(o, o, "Sony"))


def test_catches_length_drift():
    o = "Sony WH-1000XM4 headphones with 30 hours of battery life and 40mm drivers included"
    assert any("độ dài" in e for e in check(o, "Sony 30 40", "Sony"))


def test_empty_output_fails():
    assert check("anything 30", "") == ["output rỗng"]


def test_facts_hint_lists_numbers_and_units():
    h = facts_hint("Sony WH-1000XM4", "30 hours, 40mm driver, 254 g")
    assert "30" in h and "40" in h and "254" in h
    assert "mm" in h or "g" in h
