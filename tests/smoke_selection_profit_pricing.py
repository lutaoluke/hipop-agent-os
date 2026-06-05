"""WS-67 smoke: seller-settlement profit formula and luggage commission."""
from tests import test_phase1


if __name__ == "__main__":
    test_phase1.test_selection_profit_matches_ksa_pricing_table_formula_sample()
    test_phase1.test_selection_profit_path_uses_luggage_commission_and_surfaces_low_margin()
    print("OK: WS-67 profit pricing smoke")
