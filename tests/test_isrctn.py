from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from collectors.isrctn import ISRCTNCollector, _parse_full_trial


XML = """<allTrials xmlns="http://www.67bricks.com/isrctn" totalCount="1">
<fullTrial><trial lastUpdated="2026-09-25T07:18:23.431437799Z"
 publicIdentifierCanonical="ISRCTN71771495" publicIdentifierDateAssigned="2026-09-25T07:18:23Z">
 <isrctn>71771495</isrctn>
 <trialDescription><title>Public title</title><scientificTitle>Scientific title</scientificTitle>
  <primaryOutcome>Primary outcome</primaryOutcome><secondaryOutcome>Secondary outcome</secondaryOutcome></trialDescription>
 <externalRefs><clinicalTrialsGovNumber>NCT12345678</clinicalTrialsGovNumber></externalRefs>
 <trialDesign><primaryStudyDesign>Interventional</primaryStudyDesign><overallEndDate>2029-12-31T00:00:00Z</overallEndDate>
  <interventionalTrialDesign><allocation>Randomized</allocation><masking>Open</masking></interventionalTrialDesign></trialDesign>
 <participants><recruitmentCountries><country>United Kingdom</country></recruitmentCountries>
  <trialCentres><trialCentre><name>Hospital</name><city>London</city><country>England</country></trialCentre></trialCentres>
  <inclusion>Adults</inclusion><exclusion>Pregnancy</exclusion><targetEnrolment>160</targetEnrolment>
  <recruitmentStart>2026-09-14T00:00:00Z</recruitmentStart><recruitmentEnd>2028-08-15T00:00:00Z</recruitmentEnd></participants>
 <conditions><condition><description>Crohn's disease</description></condition></conditions>
 <interventions><intervention><drugNames>Drug A</drugNames><phase>Phase IV</phase></intervention></interventions>
</trial><sponsor><organisation>Imperial College London</organisation></sponsor><funder><name>Funder</name></funder></fullTrial>
</allTrials>"""


def test_isrctn_parses_and_normalises_xml():
    full = ET.fromstring(XML).find("{*}fullTrial")
    raw = _parse_full_trial(full)
    record = ISRCTNCollector().normalise(raw)
    assert record.source_trial_id == "ISRCTN71771495"
    assert record.enrollment == 160
    assert record.last_updated_at_source == "2026-09-25 07:18:23"
    assert record.study_phase == "Phase IV"
    assert json.loads(record.conditions) == ["Crohn's disease"]
    assert json.loads(record.sponsors) == ["Imperial College London", "Funder"]
    assert json.loads(record.locations)[0]["city"] == "London"
    assert raw["secondary_ids"] == ["NCT12345678"]
    assert "<ns0:fullTrial" in raw["_raw_xml"]


def test_isrctn_fetch_uses_incremental_query(monkeypatch):
    class Response:
        content = XML.encode()
        def raise_for_status(self): pass
    captured = {}
    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()
    monkeypatch.setattr("collectors.isrctn.requests.get", get)
    collector = ISRCTNCollector()
    collector.cfg.extra["query_cond"] = "heart failure"
    rows = collector.fetch_new_or_updated("2026-09-01 00:00:00")
    assert len(rows) == 1
    assert captured["params"]["q"] == '("heart failure") AND lastEdited GE 2026-09-01T00:00:00'
