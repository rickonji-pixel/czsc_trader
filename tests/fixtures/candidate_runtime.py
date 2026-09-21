"""Acceptance-only parameterized SRT using a non-OHLCV input.

Tests install this source in an isolated SRT package path. It is never a
production strategy or registered frozen release.
"""

import pandas as pd
from strategy_runtime import ExecutionPolicy, StrategyCandidate
from strategy_runtime.models import (
    CutoffRule,
    DecisionContract,
    ImplementationRef,
    InputContract,
    InputRequirement,
    MonitoringPolicy,
    ParameterSet,
    RequiredCapabilities,
    RuntimeDefinition,
)
from strategy_runtime.models import StrategyDecision


class CandidateFixture:
    def __init__(self, identity):
        candidate = isinstance(identity, StrategyCandidate)
        payload = identity.payload
        ref = payload["runtime"]
        self.definition = RuntimeDefinition(
            schema_version=2,
            strategy_family_id=identity.strategy_family_id,
            version=None if candidate else identity.version,
            release_id=identity.reference_id if candidate else identity.release_id,
            release_hash=identity.runtime_identity_sha256 if candidate else identity.release_hash,
            implementation=ImplementationRef(
                ref["module"],
                ref["qualname"],
                ref["contract_version"],
                ref["source_sha256"],
            ),
            parameters=ParameterSet(payload["parameters"]),
            inputs=InputContract(
                (
                    InputRequirement(
                        "flow",
                        "etf.share",
                        "588080.SH",
                        "daily",
                        1,
                        CutoffRule.SIGNAL_SESSION,
                    ),
                ) + ((InputRequirement(
                    "calendar", "calendar.trading_sessions", "SSE", "daily", 0,
                    CutoffRule.LATEST_AVAILABLE,
                ),) if payload["parameters"].get("with_calendar") else ())
            ),
            decision=DecisionContract("TARGET_POSITION", 0.0, 1.0, "NEXT_SESSION"),
            execution=ExecutionPolicy(
                "FROZEN_RULE",
                {
                    "capital": {
                        "fee_rate": 0.001,
                        "mode": "full_available_cash",
                        "target_scope": "entry_cycle",
                    },
                    "entry": {"limit_parameter": payload["parameters"].get("entry_premium", 0.0), "order_type": "LIMIT"},
                    "exit": {"limit_ratio": 0.1, "order_type": "MARKET"},
                    "instrument": {
                        "lot_size": 100,
                        "maximum_order_quantity": 1_000_000,
                        "price_limit_ratio": 0.1,
                        "price_tick": 0.001,
                    },
                },
            ),
            monitoring=MonitoringPolicy("OBSERVE", {}),
            capabilities=RequiredCapabilities(
                ("etf.share",) + (("calendar.trading_sessions",) if payload["parameters"].get("with_calendar") else ()),
                ("LIMIT", "MARKET"),
            ),
            identity_kind="CANDIDATE" if candidate else "RELEASE",
            candidate_id=identity.candidate_id if candidate else None,
        )

    @classmethod
    def from_candidate(cls, candidate):
        return cls(candidate)

    @classmethod
    def from_release(cls, release):
        return cls(release)

    def publish_data(self, dataflows, deployment, through):
        raise NotImplementedError("fixture only exercises pre-published historical calculation")

    def calculate_history(self, inputs, sessions):
        threshold = float(self.definition.parameters.values["threshold"])
        flow = inputs["flow"].copy()
        flow["Date"] = pd.to_datetime(flow["Date"])
        values = flow.set_index("Date").reindex(sessions)["Flow"].astype(float)
        if values.isna().any():
            raise ValueError("fixture flow does not cover the calculation sessions")
        target = (values > threshold).astype(float)
        if self.definition.parameters.values.get("invert", False):
            target = 1.0 - target
        return pd.DataFrame({"target_position": target}, index=sessions)

    def calculate(self, request):
        frame = request.publication.input_results["flow"].dataframe
        target = float(frame.iloc[-1]["Flow"] > self.definition.parameters.values["threshold"])
        if self.definition.parameters.values.get("invert", False):
            target = 1.0 - target
        definition = self.definition
        return StrategyDecision(
            f"fixture-{request.calculation_time.isoformat()}",
            request.deployment.deployment_id,
            definition.release_id,
            definition.release_hash,
            definition.runtime_sha256,
            request.calculation_time,
            request.calculation_time + pd.Timedelta(days=1),
            target,
            request.account.revision,
            request.state.revision,
            {"flow": request.publication.input_results["flow"].identity.content_sha256},
            {},
            {},
        )

    def explain(self, decision):
        return {"target": decision.target_position}
