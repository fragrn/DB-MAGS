"""M1 tests and acceptance checks for IncidentSpec and provenance."""

from __future__ import annotations

from db_repro_agent.models.incident import (
    EvidenceItem,
    IncidentSpec,
    InformationSource,
    MissingInformation,
    merge_incident_specs,
)


def post_fact(field: str, value, evidence: str) -> EvidenceItem:
    return EvidenceItem(
        field=field,
        value=value,
        source=InformationSource.POST_FACT,
        evidence=evidence,
        confidence=1.0,
    )


def agent_fact(field: str, value, evidence: str) -> EvidenceItem:
    return EvidenceItem(
        field=field,
        value=value,
        source=InformationSource.AGENT_INFERENCE,
        evidence=evidence,
        confidence=0.7,
    )


def human_fact(field: str, value, evidence: str) -> EvidenceItem:
    return EvidenceItem(
        field=field,
        value=value,
        source=InformationSource.HUMAN_INPUT,
        evidence=evidence,
        confidence=1.0,
    )


def test_complete_slow_query_fixture_round_trips() -> None:
    incident = IncidentSpec(
        incident_summary="A query becomes slow because a filtering column is not indexed.",
        dbms=post_fact("dbms", "mysql", "The post says MySQL."),
        version=post_fact("version", "8.0", "The post reports MySQL 8.0."),
        schema_facts=[post_fact("table", "users", "The SQL reads from users.")],
        data_facts=[post_fact("row_count", 1350, "The post reports about 1350 rows.")],
        sql=[post_fact("query", "SELECT * FROM users WHERE email = ?", "SQL block in post.")],
        configuration_facts=[],
        workload_facts=[post_fact("concurrency", 1, "The query is run individually.")],
        symptoms=[post_fact("symptom", "slow_query", "The post reports a 15s latency.")],
        root_cause_hypotheses=[
            post_fact("root_cause", "missing_index", "The accepted answer identifies an index.")
        ],
    )

    restored = IncidentSpec.model_validate_json(incident.model_dump_json())

    assert restored == incident
    assert restored.data_facts[0].source == InformationSource.POST_FACT
    assert restored.root_cause_hypotheses[0].value == "missing_index"


def test_missing_data_scale_preserves_criticality() -> None:
    incident = IncidentSpec(
        incident_summary="The query is slow, but the post omits the data scale.",
        sql=[post_fact("query", "SELECT * FROM orders WHERE customer_id = ?", "SQL in post.")],
        symptoms=[post_fact("symptom", "slow_query", "Latency is high in the report.")],
        missing_information=[
            MissingInformation(
                field="data_scale",
                reason="The post does not state row count or scale factor.",
                critical=True,
                impact="The scan cost cannot be calibrated reliably.",
            ),
            MissingInformation(
                field="think_time",
                reason="The background workload is not described.",
                critical=False,
                impact="Workload fidelity may be lower.",
            ),
        ],
    )

    assert {item.field: item.critical for item in incident.missing_information} == {
        "data_scale": True,
        "think_time": False,
    }
    assert incident.missing_information[0].impact.startswith("The scan")


def test_post_fact_and_agent_inference_conflict_are_both_retained() -> None:
    base = IncidentSpec(
        incident_summary="The post gives a row count.",
        data_facts=[post_fact("row_count", 1350, "Post says about 1350 rows.")],
    )
    inferred = IncidentSpec(
        incident_summary="Agent inferred a larger test scale.",
        data_facts=[agent_fact("row_count", 100000, "Large table inferred from scan behavior.")],
    )

    merged = merge_incident_specs(base, inferred)
    row_counts = {(item.source, item.value) for item in merged.data_facts}

    assert (InformationSource.POST_FACT, 1350) in row_counts
    assert (InformationSource.AGENT_INFERENCE, 100000) in row_counts
    assert len(merged.data_facts) == 2


def test_human_input_is_separately_traceable_and_resolves_missing_field() -> None:
    base = IncidentSpec(
        incident_summary="The post omits the requested reproduction scale.",
        data_facts=[post_fact("row_count", 1350, "Post says about 1350 rows.")],
        missing_information=[
            MissingInformation(
                field="target_row_count",
                reason="The reproduction scale is unspecified.",
                critical=True,
                impact="Preparation cannot choose a deterministic data size.",
            )
        ],
    )
    human = IncidentSpec(
        incident_summary="Human supplies the desired reproduction scale.",
        data_facts=[human_fact("target_row_count", 1500, "Experiment owner confirms 1500 rows.")],
    )

    merged = merge_incident_specs(base, human)
    supplied = [item for item in merged.data_facts if item.field == "target_row_count"]

    assert len(supplied) == 1
    assert supplied[0].source == InformationSource.HUMAN_INPUT
    assert supplied[0].value == 1500
    assert not any(item.field == "target_row_count" for item in merged.missing_information)
    assert any(item.field == "row_count" and item.source == InformationSource.POST_FACT for item in merged.data_facts)


def test_duplicate_evidence_is_deduplicated_without_dropping_conflicts() -> None:
    base = IncidentSpec(
        incident_summary="Duplicate evidence test.",
        symptoms=[post_fact("symptom", "slow_query", "same source")],
    )
    supplement = IncidentSpec(
        incident_summary="Duplicate evidence test.",
        symptoms=[
            post_fact("symptom", "slow_query", "same source"),
            agent_fact("symptom", "qps_drop", "different inference"),
        ],
    )

    merged = merge_incident_specs(base, supplement)

    assert len(merged.symptoms) == 2
    assert {(item.source, item.value) for item in merged.symptoms} == {
        (InformationSource.POST_FACT, "slow_query"),
        (InformationSource.AGENT_INFERENCE, "qps_drop"),
    }


def test_scalar_provenance_conflict_is_recorded() -> None:
    base = IncidentSpec(
        incident_summary="DBMS conflict.",
        dbms=post_fact("dbms", "mysql", "Post says MySQL."),
    )
    supplement = IncidentSpec(
        incident_summary="DBMS conflict.",
        dbms=agent_fact("dbms", "postgres", "Agent inferred PostgreSQL."),
    )

    merged = merge_incident_specs(base, supplement)

    assert merged.dbms is not None
    assert merged.dbms.value == "mysql"
    assert {(item.source, item.value) for item in merged.provenance_conflicts} == {
        (InformationSource.POST_FACT, "mysql"),
        (InformationSource.AGENT_INFERENCE, "postgres"),
    }
