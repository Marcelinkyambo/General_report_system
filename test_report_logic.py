import unittest

import pandas as pd

from report_logic import (
    apply_reorder_flags,
    apply_stock_coverage,
    derive_stock_components,
    find_closing_stock_quantity_column,
)


class ReorderFlagTests(unittest.TestCase):
    def test_stock_upload_column_selection_prefers_closing_stock_quantity(self):
        columns = [
            "quantity_in",
            "quantity_in_amount",
            "closing_stock",
            "closing_stock_amount",
        ]

        self.assertEqual(find_closing_stock_quantity_column(columns), "closing_stock")

    def test_stock_upload_column_selection_requires_closing_stock_quantity(self):
        columns = [
            "quantity_in",
            "stock",
            "stock_amount",
        ]

        self.assertIsNone(find_closing_stock_quantity_column(columns))

    def test_stock_upload_column_selection_ignores_closing_stock_amount(self):
        columns = [
            "Quantity In",
            "Quantity In Amount",
            "Closing Stock Amount",
            "Closing Stock",
        ]

        self.assertEqual(find_closing_stock_quantity_column(columns), "Closing Stock")

    def test_stock_components_use_uploaded_bonded_and_in_transit_sources(self):
        report_input = pd.DataFrame(
            {
                "total_stock": [840, 753],
                "twc_stock": [0, 3],
                "bonded_stock_uploaded": [pd.NA, 300],
                "in_transit_qty": [840, 450],
                "monthly_run_rate": [10, 10],
                "weekly_run_rate": [2, 2],
            },
            index=["transit_only_upload", "separate_bonded_and_transit_uploads"],
        )

        result = apply_stock_coverage(derive_stock_components(report_input))

        self.assertEqual(result.loc["transit_only_upload", "bonded_stock"], 0)
        self.assertEqual(result.loc["transit_only_upload", "in_transit_qty"], 840)
        self.assertEqual(result.loc["transit_only_upload", "total_stock_for_coverage"], 840)
        self.assertEqual(result.loc["separate_bonded_and_transit_uploads", "bonded_stock"], 300)
        self.assertEqual(result.loc["separate_bonded_and_transit_uploads", "in_transit_qty"], 450)
        self.assertEqual(result.loc["separate_bonded_and_transit_uploads", "total_stock_for_coverage"], 753)

    def test_cleared_stock_is_total_less_bonded_and_in_transit(self):
        report_input = pd.DataFrame(
            {
                "total_stock": [589],
                "twc_stock": [1],
                "bonded_stock_uploaded": [420],
                "in_transit_qty": [0],
            }
        )

        result = derive_stock_components(report_input)

        self.assertEqual(result.loc[0, "twc_stock"], 169)
        self.assertEqual(result.loc[0, "bonded_stock"], 420)
        self.assertEqual(result.loc[0, "in_transit_qty"], 0)

    def test_stock_coverage_uses_bonded_in_transit_and_current_sales(self):
        report_input = pd.DataFrame(
            {
                "total_stock": [20],
                "twc_stock": [20],
                "bonded_stock": [30],
                "in_transit_qty": [10],
                "monthly_run_rate": [10],
                "weekly_run_rate": [2.5],
            }
        )

        result = apply_stock_coverage(report_input)

        self.assertEqual(result.loc[0, "total_stock_for_coverage"], 60)
        self.assertEqual(result.loc[0, "cleared_stock_for_coverage"], 20)
        self.assertEqual(result.loc[0, "total_stock_coverage_months"], 6)
        self.assertEqual(result.loc[0, "total_stock_coverage_weeks"], 24)
        self.assertEqual(result.loc[0, "twc_coverage_months"], 2)
        self.assertEqual(result.loc[0, "twc_coverage_weeks"], 8)

    def test_reorder_estimates_use_component_stock_coverage_basis(self):
        report_input = pd.DataFrame(
            {
                "total_stock_coverage_months": [2],
                "twc_coverage_months": [0.5],
                "stockout_risk": ["HIGH"],
                "bonded_stock": [20],
                "weekly_run_rate": [4],
                "quantity_sold": [50],
                "annual_target": [250],
                "remaining_year_demand": [120],
                "total_stock": [10],
                "twc_stock": [5],
                "total_stock_for_coverage": [35],
                "cleared_stock_for_coverage": [15],
            }
        )

        result = apply_reorder_flags(report_input, remaining_weeks=30)

        self.assertEqual(result.loc[0, "bonded_est_reorder_qty"], 110)
        self.assertEqual(result.loc[0, "twc_est_reorder_qty"], 14)

    def test_twc_reorder_requires_bonded_stock_for_item(self):
        report_input = pd.DataFrame(
            {
                "total_stock_coverage_months": [0.5, 0.5],
                "twc_coverage_months": [0.5, 0.5],
                "bonded_stock": [0, 8],
                "weekly_run_rate": [1, 1],
                "quantity_sold": [5, 5],
                "annual_target": [20, 20],
                "remaining_year_demand": [20, 20],
                "total_stock": [5, 10],
                "twc_stock": [5, 2],
                "stockout_risk": ["HIGH", "HIGH"],
            },
            index=["no_bonded_stock", "has_bonded_stock"],
        )

        result = apply_reorder_flags(report_input, remaining_weeks=10)

        self.assertEqual(result.loc["no_bonded_stock", "bonded_reorder"], "REORDER")
        self.assertEqual(result.loc["no_bonded_stock", "twc_reorder"], "")
        self.assertEqual(result.loc["no_bonded_stock", "twc_est_reorder_qty"], 0)

        self.assertEqual(result.loc["has_bonded_stock", "bonded_reorder"], "REORDER")
        self.assertEqual(result.loc["has_bonded_stock", "twc_reorder"], "CLEAR")
        self.assertGreater(result.loc["has_bonded_stock", "twc_est_reorder_qty"], 0)

    def test_bonded_estimate_uses_higher_of_run_rate_and_annual_target_demand(self):
        report_input = pd.DataFrame(
            {
                "total_stock_coverage_months": [4],
                "twc_coverage_months": [2],
                "bonded_stock": [40],
                "weekly_run_rate": [2],
                "quantity_sold": [20],
                "annual_target": [220],
                "remaining_year_demand": [80],
                "total_stock": [60],
                "twc_stock": [20],
                "stockout_risk": ["MEDIUM"],
            }
        )

        result = apply_reorder_flags(report_input, remaining_weeks=40)

        self.assertEqual(result.loc[0, "bonded_reorder"], "REORDER")
        self.assertEqual(result.loc[0, "bonded_est_reorder_qty"], 49)

    def test_twc_estimate_is_capped_by_available_bonded_stock(self):
        report_input = pd.DataFrame(
            {
                "total_stock_coverage_months": [2],
                "twc_coverage_months": [0.5],
                "bonded_stock": [12],
                "weekly_run_rate": [4],
                "quantity_sold": [50],
                "annual_target": [250],
                "remaining_year_demand": [120],
                "total_stock": [30],
                "twc_stock": [5],
                "stockout_risk": ["HIGH"],
            }
        )

        result = apply_reorder_flags(report_input, remaining_weeks=30)

        self.assertEqual(result.loc[0, "twc_reorder"], "CLEAR")
        self.assertEqual(result.loc[0, "twc_est_reorder_qty"], 12)

    def test_bonded_reorder_uses_total_stock_coverage_threshold(self):
        report_input = pd.DataFrame(
            {
                "total_stock_coverage_months": [5, 5.1, 4.9],
                "twc_coverage_months": [2, 2, 2],
                "bonded_stock": [0, 0, 0],
                "weekly_run_rate": [4, 4, 4],
                "quantity_sold": [50, 50, 50],
                "annual_target": [250, 250, 250],
                "remaining_year_demand": [120, 120, 120],
                "total_stock": [30, 30, 30],
                "twc_stock": [5, 5, 5],
                "stockout_risk": ["", "HIGH", ""],
            }
        )

        result = apply_reorder_flags(report_input, remaining_weeks=30)

        self.assertEqual(result.loc[0, "bonded_reorder"], "REORDER")
        self.assertEqual(result.loc[1, "bonded_reorder"], "")
        self.assertEqual(result.loc[2, "bonded_reorder"], "REORDER")

    def test_bonded_reorder_is_blank_when_item_has_in_transit_stock(self):
        report_input = pd.DataFrame(
            {
                "total_stock_coverage_months": [4, 4],
                "twc_coverage_months": [2, 2],
                "bonded_stock": [0, 0],
                "in_transit_qty": [0, 6],
                "weekly_run_rate": [4, 4],
                "quantity_sold": [50, 50],
                "annual_target": [250, 250],
                "remaining_year_demand": [120, 120],
                "total_stock": [30, 30],
                "twc_stock": [5, 5],
            }
        )

        result = apply_reorder_flags(report_input, remaining_weeks=30)

        self.assertEqual(result.loc[0, "bonded_reorder"], "REORDER")
        self.assertEqual(result.loc[0, "bonded_est_reorder_qty"], 115)
        self.assertEqual(result.loc[1, "bonded_reorder"], "")
        self.assertEqual(result.loc[1, "bonded_est_reorder_qty"], 0)

    def test_twc_reorder_uses_twc_coverage_threshold(self):
        report_input = pd.DataFrame(
            {
                "total_stock_coverage_months": [10, 10, 10],
                "twc_coverage_months": [1, 1.1, 0.9],
                "bonded_stock": [10, 10, 10],
                "weekly_run_rate": [4, 4, 4],
                "quantity_sold": [50, 50, 50],
                "annual_target": [250, 250, 250],
                "remaining_year_demand": [120, 120, 120],
                "total_stock": [60, 60, 60],
                "twc_stock": [5, 5, 5],
                "stockout_risk": ["", "", ""],
            }
        )

        result = apply_reorder_flags(report_input, remaining_weeks=30)

        self.assertEqual(result.loc[0, "twc_reorder"], "CLEAR")
        self.assertEqual(result.loc[1, "twc_reorder"], "")
        self.assertEqual(result.loc[2, "twc_reorder"], "CLEAR")


if __name__ == "__main__":
    unittest.main()
