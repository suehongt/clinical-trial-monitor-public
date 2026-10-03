"""ClinicalTrials.gov collector normalisation tests.

Regression for the multi-primary-endpoint data loss: normalise() used to
keep only primaryOutcomes[0], silently dropping any additional primary
outcome measures the study declared.
"""
from __future__ import annotations

import json

import pytest


def _study(primary_measures, secondary_measures=None):
    outcomes = {"primaryOutcomes": [{"measure": m} for m in primary_measures]}
    if secondary_measures is not None:
        outcomes["secondaryOutcomes"] = [{"measure": m} for m in secondary_measures]
    return {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT00000000"},
            "statusModule": {"overallStatus": "RECRUITING"},
            "outcomesModule": outcomes,
        }
    }


@pytest.fixture
def collector():
    from collectors.clinicaltrials import ClinicalTrialsGovCollector

    return ClinicalTrialsGovCollector()


def test_single_primary_stays_plain_text(collector):
    rec = collector.normalise(_study(["Overall survival"]))
    assert rec.primary_endpoint == "Overall survival"


def test_multiple_primary_endpoints_all_kept(collector):
    rec = collector.normalise(_study(["Pathological complete response", "Event-free survival"]))
    assert json.loads(rec.primary_endpoint) == [
        "Pathological complete response",
        "Event-free survival",
    ]


def test_no_primary_endpoints_is_none(collector):
    rec = collector.normalise(_study([]))
    assert rec.primary_endpoint is None


def test_secondary_endpoints_stay_json_array(collector):
    rec = collector.normalise(_study(["OS"], ["PFS", "QoL"]))
    assert json.loads(rec.secondary_endpoints) == ["PFS", "QoL"]
