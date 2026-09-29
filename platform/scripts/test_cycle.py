#!/usr/bin/env python3
"""End-to-end test: Survey → Planning → Analysis → Survey#2 cycle.

Calls service/runtime layers directly — no HTTP server needed.
Requires: PostgreSQL+pgvector running, .env configured with ANTHROPIC_API_KEY.

Usage:
    # 1. Start dev infra
    docker compose -f docker-compose.dev.yml up -d

    # 2. Apply migrations
    cd backend && alembic upgrade head

    # 3. Run this script
    cd .. && python scripts/test_cycle.py

    # Optional flags:
    #   --skip-survey   Skip Survey (use mock), start from Planning
    #   --dry-run       Set up project + graph only, don't execute nodes
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Add backend to path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Must set env before importing app modules
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/research_platform")


# ─── Helpers ─────────────────────────────────────────────────────────────────

class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    END = "\033[0m"

def step(msg: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.BLUE}>>> {msg}{Colors.END}")

def ok(msg: str) -> None:
    print(f"  {Colors.GREEN}OK{Colors.END} {msg}")

def warn(msg: str) -> None:
    print(f"  {Colors.YELLOW}WARN{Colors.END} {msg}")

def fail(msg: str) -> None:
    print(f"  {Colors.RED}FAIL{Colors.END} {msg}")

def info(msg: str) -> None:
    print(f"  {msg}")


# ─── Core test logic ────────────────────────────────────────────────────────

async def run_test(*, skip_survey: bool = False, dry_run: bool = False, mock: bool = False) -> None:
    # ── 0. Bootstrap ─────────────────────────────────────────────────────
    step("Bootstrapping application")

    from app.config import settings
    from app.database import get_session_factory
    from app.tools import register_all_tool_executors

    # Verify API key (skip check in mock mode)
    if mock:
        ok("Mock mode — API key check skipped")
    else:
        provider = settings.default_llm_provider
        if provider == "deepseek" and settings.deepseek_api_key:
            ok(f"DeepSeek API key configured (ends ...{settings.deepseek_api_key[-6:]})")
        elif provider == "anthropic" and settings.anthropic_api_key:
            ok(f"Anthropic API key configured (ends ...{settings.anthropic_api_key[-6:]})")
        elif settings.deepseek_api_key or settings.anthropic_api_key:
            ok(f"LLM provider: {provider} (fallback available)")
        else:
            fail("No LLM API key configured in .env")
            sys.exit(1)

    # Register tool executors (normally done in FastAPI lifespan)
    register_all_tool_executors()
    ok("Tool executors registered")

    # Install mock LLM if requested (must be after imports, before execution)
    if mock:
        install_mock_llm()

    # Test DB connection
    factory = get_session_factory()
    try:
        async with factory() as db:
            from sqlalchemy import text
            await db.execute(text("SELECT 1"))
            # Check pgvector
            result = await db.execute(text("SELECT extname FROM pg_extension WHERE extname = 'vector'"))
            has_vector = result.scalar_one_or_none()
            if not has_vector:
                warn("pgvector extension not found — KB search will fail. Run: CREATE EXTENSION vector;")
            else:
                ok("pgvector extension available")
        ok("Database connection OK")
    except Exception as e:
        fail(f"Database connection failed: {e}")
        info("Run: docker compose -f docker-compose.dev.yml up -d")
        info("Then: cd backend && alembic upgrade head")
        sys.exit(1)

    # ── 1. Create user + project ─────────────────────────────────────────
    step("Creating test user and project")

    from app.auth import hash_password
    from app.models.user import User
    from app.services.project_service import create_project_with_seed_graph
    from app.models.project import EntryType

    async with factory() as db:
        # Create test user (or reuse existing)
        from sqlalchemy import select
        existing = await db.execute(select(User).where(User.email == "test@cycle.dev"))
        user = existing.scalar_one_or_none()
        if not user:
            user = User(
                email="test@cycle.dev",
                hashed_password=hash_password("test1234"),
                display_name="Cycle Tester",
            )
            db.add(user)
            await db.flush()
            ok(f"Created user: {user.email} (id={user.id})")
        else:
            ok(f"Reusing user: {user.email} (id={user.id})")

        # Create project
        result = await create_project_with_seed_graph(
            db=db,
            owner_id=user.id,
            name="Surrogate models for turbulent transition prediction",
            description=(
                "Investigate data-driven surrogate models (ANN-based) for predicting "
                "laminar-turbulent transition in boundary layers. Focus on replacing "
                "RANS transition model intermittency equations with neural network surrogates. "
                "Key variables: Reynolds number, turbulence intensity, pressure gradient."
            ),
            research_domain="Computational Fluid Dynamics",
            entry_type=EntryType.FUZZY_IDEA,
        )
        await db.commit()

        project = result["project"]
        nodes = result["nodes"]
        edges = result["edges"]

    project_id = str(project.id)
    user_id = str(user.id)
    ok(f"Project created: {project.name}")
    ok(f"  id={project_id}")
    ok(f"  entry_type=fuzzy_idea → seed graph: {len(nodes)} nodes, {len(edges)} edges")
    for n in nodes:
        info(f"    [{n.type.value}] {n.title} (id={n.id}, status={n.status.value})")

    if dry_run:
        step("DRY RUN — stopping before execution")
        return

    # ── 2. Execute nodes in sequence ─────────────────────────────────────
    from app.core.graph_runtime import graph_runtime
    from app.core.harness.executor import HarnessExecutor
    from app.core.budget_manager import BudgetManager
    from app.models.budget import BudgetType, ConsumptionSource
    from app.models.graph import Node, Edge, NodeStatus, EdgeType

    async def execute_node(
        node_id: str,
        task_desc: str | None = None,
        extra_inputs: dict | None = None,
    ) -> dict:
        """Execute a single node — mirrors the API endpoint logic."""
        async with factory() as db:
            # Fetch node
            result = await db.execute(
                select(Node).where(Node.id == node_id, Node.project_id == project_id)
            )
            node = result.scalar_one_or_none()
            if not node:
                fail(f"Node {node_id} not found")
                return {"success": False, "error": "not found"}

            info(f"  Status: {node.status.value} → active")

            # Set ACTIVE
            node.status = NodeStatus.ACTIVE
            node.started_at = datetime.now(timezone.utc)
            await db.flush()

            # Build inputs
            inputs: dict = {}
            if task_desc:
                inputs["task_description"] = task_desc
            elif node.description:
                inputs["task_description"] = node.description
            if extra_inputs:
                inputs.update(extra_inputs)

            # Auto-fetch upstream handoff
            if "handoff" not in inputs:
                upstream_edges = await db.execute(
                    select(Edge).where(
                        Edge.target_node_id == node_id,
                        Edge.type.in_([EdgeType.DEPENDS_ON, EdgeType.ITERATES]),
                    )
                )
                source_ids = [str(e.source_node_id) for e in upstream_edges.scalars().all()]
                if source_ids:
                    sources = await db.execute(
                        select(Node).where(
                            Node.id.in_(source_ids),
                            Node.status == NodeStatus.COMPLETED,
                        )
                    )
                    upstream_nodes = list(sources.scalars().all())
                    if upstream_nodes:
                        handoff_parts = []
                        for up in upstream_nodes:
                            meta = up.execution_metadata or {}
                            if meta.get("handoff_content"):
                                handoff_parts.append({
                                    "node_type": up.type.value,
                                    "title": up.title,
                                    "handoff": meta["handoff_content"],
                                })
                            elif meta.get("raw_response"):
                                handoff_parts.append({
                                    "node_type": up.type.value,
                                    "title": up.title,
                                    "handoff": {"raw_response": meta["raw_response"]},
                                })
                        if len(handoff_parts) == 1:
                            inputs["handoff"] = handoff_parts[0]
                        elif handoff_parts:
                            inputs["handoff"] = {"upstream_nodes": handoff_parts}
                        if handoff_parts:
                            ok(f"  Auto-handoff from {len(handoff_parts)} upstream node(s)")

            # Budget callbacks
            budget_mgr = BudgetManager()

            async def budget_check():
                return await budget_mgr.check_budget(project_id, BudgetType.LLM_TOKENS, 0, db)

            async def budget_record(input_tokens: int, output_tokens: int, cost: float):
                await budget_mgr.record_consumption(
                    project_id=project_id,
                    budget_type=BudgetType.LLM_TOKENS,
                    source=ConsumptionSource.LLM_CALL,
                    amount=input_tokens + output_tokens,
                    node_id=node_id,
                    details={"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost},
                    db=db,
                )

            # Execute
            executor = HarnessExecutor()
            exec_result = await executor.execute(
                node_type=node.type.value,
                node_id=node_id,
                project_id=project_id,
                inputs=inputs,
                node_overrides=node.harness_config,
                is_autonomous=False,
                budget_check_fn=budget_check,
                budget_record_fn=budget_record,
                db=db,
                user_id=user_id,
            )

            # Update node status
            # In mock mode, force COMPLETED even if completion criteria aren't met
            # (mock LLM doesn't call tools, so required_outputs won't be produced)
            force_complete = mock
            if exec_result.success or force_complete:
                node.status = NodeStatus.COMPLETED
                node.completed_at = datetime.now(timezone.utc)
            elif exec_result.review_triggered:
                node.status = NodeStatus.PAUSED
            else:
                node.status = NodeStatus.FAILED

            # Store metadata
            node.execution_metadata = {
                "token_usage": exec_result.token_usage,
                "cost_usd": exec_result.cost_usd,
                "iterations": exec_result.iterations,
                "review_triggered": exec_result.review_triggered,
                "outputs_summary": list(exec_result.outputs.keys()),
                "handoff_content": exec_result.handoff_content,
                "raw_response": (exec_result.outputs.get("raw_response") or "")[:15000],
            }
            if exec_result.outputs.get("verdict"):
                node.execution_metadata["verdict"] = exec_result.outputs["verdict"]
            await db.flush()

            # Chain reaction
            chain_result = {"promoted_to_ready": [], "growth_proposals": [], "decision_points": []}
            if exec_result.success or force_complete:
                chain_result = await graph_runtime.on_node_completed(node_id, project_id, db)

            await db.commit()

            return {
                "success": exec_result.success or force_complete,
                "node_type": node.type.value,
                "status": node.status.value,
                "iterations": exec_result.iterations,
                "tokens": exec_result.token_usage,
                "cost_usd": exec_result.cost_usd,
                "review_triggered": exec_result.review_triggered,
                "completion_met": exec_result.completion.met_criteria if exec_result.completion else [],
                "completion_unmet": exec_result.completion.unmet_criteria if exec_result.completion else [],
                "error": exec_result.error,
                "outputs_keys": list(exec_result.outputs.keys()),
                "verdict": exec_result.outputs.get("verdict"),
                "promoted_to_ready": chain_result["promoted_to_ready"],
                "growth_proposals": chain_result["growth_proposals"],
                "decision_points": chain_result["decision_points"],
            }

    # ── 2a. Survey ───────────────────────────────────────────────────────
    survey_node = nodes[0]  # fuzzy_idea seed: [Survey, Planning]
    planning_node = nodes[1]

    step(f"Executing SURVEY node: {survey_node.title}")
    info(f"  node_id={survey_node.id}")

    survey_result = await execute_node(str(survey_node.id))

    if survey_result["success"]:
        ok(f"Survey completed in {survey_result['iterations']} iterations")
        ok(f"  Cost: ${survey_result['cost_usd']:.4f}")
        ok(f"  Tokens: {survey_result['tokens']}")
        ok(f"  Outputs: {survey_result['outputs_keys']}")
        ok(f"  Completion met: {survey_result['completion_met']}")
        if survey_result["completion_unmet"]:
            warn(f"  Completion unmet: {survey_result['completion_unmet']}")
        if survey_result["review_triggered"]:
            warn(f"  Review triggered: {survey_result['review_triggered']}")
        if survey_result["promoted_to_ready"]:
            ok(f"  Promoted to READY: {survey_result['promoted_to_ready']}")
    else:
        fail(f"Survey failed: {survey_result['error']}")
        info(f"  Iterations: {survey_result['iterations']}")
        info(f"  Partial outputs: {survey_result['outputs_keys']}")
        warn("Continuing anyway — downstream nodes will have limited input")

    # ── 2b. Planning ─────────────────────────────────────────────────────
    step(f"Executing PLANNING node: {planning_node.title}")
    info(f"  node_id={planning_node.id}")

    planning_result = await execute_node(str(planning_node.id))

    if planning_result["success"]:
        ok(f"Planning completed in {planning_result['iterations']} iterations")
        ok(f"  Cost: ${planning_result['cost_usd']:.4f}")
        ok(f"  Outputs: {planning_result['outputs_keys']}")
        if planning_result["promoted_to_ready"]:
            ok(f"  Promoted to READY: {planning_result['promoted_to_ready']}")
    else:
        fail(f"Planning failed: {planning_result['error']}")

    # ── 2c. Analysis (feeds on Planning output, no Experiment yet) ───────
    # fuzzy_idea seed only has Survey → Planning, no Analysis node.
    # We need to create an Analysis node manually for the cycle test.
    step("Creating Analysis node (not in fuzzy_idea seed)")

    async with factory() as db:
        analysis_node = Node(
            project_id=project_id,
            type="analysis",
            title="Analysis: Surrogate models for turbulent transition",
            description=(
                "Analyze the survey findings and experiment plan. Evaluate hypotheses, "
                "determine if more literature survey or experiments are needed. "
                "Produce a structured verdict with needs_more_survey / needs_more_experiments flags."
            ),
            status=NodeStatus.PLANNED,
            iteration=1,
            branch_id=nodes[0].branch_id,
        )
        db.add(analysis_node)
        await db.flush()

        # Edge: Planning → Analysis (depends_on)
        edge = Edge(
            project_id=project_id,
            source_node_id=str(planning_node.id),
            target_node_id=str(analysis_node.id),
            type=EdgeType.DEPENDS_ON,
        )
        db.add(edge)
        await db.commit()

        analysis_node_id = str(analysis_node.id)
    ok(f"Analysis node created: id={analysis_node_id}")

    step(f"Executing ANALYSIS node")
    info(f"  node_id={analysis_node_id}")
    info("  (This node drives the cycle — its verdict determines if Survey#2 is created)")

    analysis_result = await execute_node(analysis_node_id)

    if analysis_result["success"]:
        ok(f"Analysis completed in {analysis_result['iterations']} iterations")
        ok(f"  Cost: ${analysis_result['cost_usd']:.4f}")
        ok(f"  Outputs: {analysis_result['outputs_keys']}")
        if analysis_result["verdict"]:
            ok(f"  Verdict extracted:")
            verdict = analysis_result["verdict"]
            info(f"    needs_more_survey: {verdict.get('needs_more_survey')}")
            info(f"    needs_more_experiments: {verdict.get('needs_more_experiments')}")
            info(f"    recommended_next_step: {verdict.get('recommended_next_step')}")
            if verdict.get("hypotheses"):
                for h in verdict["hypotheses"]:
                    info(f"    H: {h.get('hypothesis', '?')[:80]} → {h.get('status')}")
        else:
            warn("  No structured verdict extracted from analysis output")

        if analysis_result["growth_proposals"]:
            ok(f"  Growth proposals: {analysis_result['growth_proposals']}")
        if analysis_result["promoted_to_ready"]:
            ok(f"  Promoted to READY: {analysis_result['promoted_to_ready']}")
        if analysis_result["decision_points"]:
            warn(f"  Decision points: {analysis_result['decision_points']}")
    else:
        fail(f"Analysis failed: {analysis_result['error']}")

    # ── 3. Verify cycle ──────────────────────────────────────────────────
    step("Verifying cycle state")

    async with factory() as db:
        # Check all nodes
        from sqlalchemy import select
        all_nodes = await db.execute(
            select(Node).where(Node.project_id == project_id).order_by(Node.created_at)
        )
        all_nodes_list = list(all_nodes.scalars().all())

        all_edges = await db.execute(
            select(Edge).where(Edge.project_id == project_id)
        )
        all_edges_list = list(all_edges.scalars().all())

        # Check for iteration nodes
        iteration_nodes = [n for n in all_nodes_list if n.iteration > 1]

        info(f"  Total nodes: {len(all_nodes_list)}")
        info(f"  Total edges: {len(all_edges_list)}")
        for n in all_nodes_list:
            status_color = {
                "completed": Colors.GREEN,
                "ready": Colors.BLUE,
                "planned": "",
                "failed": Colors.RED,
                "paused": Colors.YELLOW,
                "active": Colors.BLUE,
            }.get(n.status.value, "")
            iter_tag = f" (iter #{n.iteration})" if n.iteration > 1 else ""
            print(f"    [{n.type.value:12s}] {status_color}{n.status.value:10s}{Colors.END} {n.title}{iter_tag}")

        print()
        for e in all_edges_list:
            src = next((n for n in all_nodes_list if str(n.id) == str(e.source_node_id)), None)
            tgt = next((n for n in all_nodes_list if str(n.id) == str(e.target_node_id)), None)
            src_label = f"{src.type.value}" if src else "?"
            tgt_label = f"{tgt.type.value}" if tgt else "?"
            tgt_iter = f" (iter #{tgt.iteration})" if tgt and tgt.iteration > 1 else ""
            print(f"    {src_label} --[{e.type.value}]--> {tgt_label}{tgt_iter}")

        if iteration_nodes:
            ok(f"CYCLE DETECTED: {len(iteration_nodes)} iteration node(s) created")
            for n in iteration_nodes:
                info(f"    {n.type.value}#{n.iteration}: {n.title} (status={n.status.value})")
        else:
            warn("No iteration nodes created — Analysis may not have requested more survey/experiments")

        # Check scheduling
        scheduling = await graph_runtime.evaluate_ready_nodes(project_id, db)
        if scheduling.ready_nodes:
            ok(f"Ready for execution: {len(scheduling.ready_nodes)} node(s)")
            for nid in scheduling.ready_nodes:
                n = next((x for x in all_nodes_list if str(x.id) == nid), None)
                if n:
                    info(f"    {n.type.value}: {n.title}")

    # ── 4. Summary ───────────────────────────────────────────────────────
    step("Test Summary")

    total_cost = sum(r.get("cost_usd", 0) for r in [survey_result, planning_result, analysis_result])
    total_iters = sum(r.get("iterations", 0) for r in [survey_result, planning_result, analysis_result])

    info(f"  Nodes executed: 3 (Survey, Planning, Analysis)")
    info(f"  Total iterations: {total_iters}")
    info(f"  Total cost: ${total_cost:.4f}")
    info(f"  Cycle created: {'YES' if iteration_nodes else 'NO'}")

    results_all_ok = all(r.get("success") for r in [survey_result, planning_result, analysis_result])
    if results_all_ok and iteration_nodes:
        print(f"\n{Colors.GREEN}{Colors.BOLD}ALL PASS — full cycle validated{Colors.END}")
    elif results_all_ok:
        print(f"\n{Colors.YELLOW}{Colors.BOLD}PARTIAL — nodes OK but no cycle triggered (verdict may not have requested it){Colors.END}")
    else:
        failed_nodes = [r["node_type"] for r in [survey_result, planning_result, analysis_result] if not r.get("success")]
        print(f"\n{Colors.RED}{Colors.BOLD}FAILED — these nodes failed: {failed_nodes}{Colors.END}")


# ─── Mock LLM provider ──────────────────────────────────────────────────────

def install_mock_llm() -> None:
    """Replace the LLM router's provider with a mock that returns canned responses.

    Each node type gets a realistic response so the full cycle logic is exercised:
    - Survey: returns literature summary
    - Planning: returns experiment plan
    - Analysis: returns structured verdict with needs_more_survey=True
    """
    from app.llm.base import LLMConfig, LLMMessage, LLMProvider, LLMResponse
    from app.llm.router import llm_router

    MOCK_SURVEY = (
        "# Literature Survey: Surrogate Models for Turbulent Transition Prediction\n\n"
        "## Key Findings\n"
        "1. **Neural network surrogates** have shown promise replacing RANS transition models "
        "(Duraisamy et al., 2019; Brunton et al., 2020).\n"
        "2. Physics-informed neural networks (PINNs) can enforce boundary-layer conservation laws "
        "while maintaining data-driven flexibility.\n"
        "3. The γ-Reθ transition model intermittency equation is the primary candidate for surrogate "
        "replacement, as identified by Menter et al. (2006).\n"
        "4. Current datasets are limited to flat-plate and airfoil geometries — no publicly available "
        "3D transition datasets for validation.\n"
        "5. Transfer learning from DNS data to RANS-scale predictions remains an open challenge.\n\n"
        "## Gaps Identified\n"
        "- No comprehensive comparison of ANN architectures for transition prediction\n"
        "- Uncertainty quantification for surrogate predictions is understudied\n"
        "- Pressure gradient effects on surrogate accuracy need systematic investigation\n"
    )

    MOCK_PLANNING = (
        "# Research Plan: ANN Surrogates for Transition Prediction\n\n"
        "## Phase 1: Data Generation\n"
        "- Run RANS simulations with γ-Reθ model on flat plate + NACA 0012 cases\n"
        "- Vary: Re ∈ [5e5, 5e7], Tu ∈ [0.1%, 6%], dP/dx ∈ [-0.03, 0.03]\n"
        "- Target: 10,000 training samples\n\n"
        "## Phase 2: Model Development\n"
        "- Architectures: MLP, LSTM, Transformer-based\n"
        "- Input features: Re_θ, Tu, dP/dx, wall distance\n"
        "- Output: intermittency γ\n\n"
        "## Phase 3: Validation\n"
        "- Cross-validation on held-out geometries (T3A, T3B flat plate series)\n"
        "- Compare C_f and transition location against experimental data\n"
    )

    MOCK_ANALYSIS = (
        "# Analysis: Surrogate Model Feasibility Assessment\n\n"
        "## Findings\n"
        "The literature survey reveals strong theoretical foundations for ANN-based transition "
        "surrogates, but several critical gaps remain unaddressed:\n\n"
        "1. **Data availability**: No standardized benchmark dataset exists for training transition surrogates.\n"
        "2. **Architecture comparison**: Prior work focuses on individual architectures without systematic comparison.\n"
        "3. **Generalization**: Transfer across Reynolds number regimes is untested.\n\n"
        "## Hypothesis Evaluation\n"
        "- H1 (ANN can predict γ): PARTIALLY SUPPORTED — works for simple geometries, unclear for complex flows.\n"
        "- H2 (PINNs outperform pure data-driven): UNTESTED — insufficient comparative studies.\n\n"
        "## Structured Verdict\n"
        "```json\n"
        '{\n'
        '  "verdict": {\n'
        '    "needs_more_survey": true,\n'
        '    "needs_more_experiments": false,\n'
        '    "recommended_next_step": "Targeted survey on PINN architectures for transition prediction and UQ methods",\n'
        '    "confidence": "medium",\n'
        '    "hypotheses": [\n'
        '      {"hypothesis": "ANN can replace intermittency equation", "status": "partially_supported"},\n'
        '      {"hypothesis": "PINNs provide better generalization", "status": "untested"}\n'
        '    ]\n'
        '  }\n'
        '}\n'
        "```\n"
    )

    class MockProvider(LLMProvider):
        """Returns canned responses based on detected node type."""

        _call_count = 0

        async def chat(
            self,
            messages: list[LLMMessage],
            config: LLMConfig | None = None,
            tools: list[dict] | None = None,
        ) -> LLMResponse:
            self._call_count += 1
            # Detect node type from system prompt
            system_text = ""
            for m in messages:
                if m.role == "system" and isinstance(m.content, str):
                    system_text = m.content.lower()
                    break

            # Match most specific first to avoid false positives
            if "data analyst" in system_text or "hypothesis verifier" in system_text:
                content = MOCK_ANALYSIS
                info(f"  [mock] Detected: ANALYSIS (call #{self._call_count})")
            elif "research planner" in system_text:
                content = MOCK_PLANNING
                info(f"  [mock] Detected: PLANNING (call #{self._call_count})")
            elif "literature survey agent" in system_text:
                content = MOCK_SURVEY
                info(f"  [mock] Detected: SURVEY (call #{self._call_count})")
            else:
                content = "Mock response — node type not recognized in system prompt."
                warn(f"  [mock] UNRECOGNIZED node type (call #{self._call_count})")

            return LLMResponse(
                content=content,
                model="mock-model",
                input_tokens=500,
                output_tokens=300,
                total_tokens=800,
                cost_usd=0.0,
                stop_reason="end_turn",
                tool_calls=[],
                raw_content=[],
            )

        def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
            return 0.0

        def count_tokens(self, text: str) -> int:
            return len(text) // 3

    mock = MockProvider()
    llm_router._providers = {"anthropic": mock}
    ok("Mock LLM provider installed (no API calls will be made)")


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    skip_survey = "--skip-survey" in sys.argv
    dry_run = "--dry-run" in sys.argv
    mock_mode = "--mock" in sys.argv

    print(f"{Colors.HEADER}{Colors.BOLD}")
    print("=" * 60)
    print("  Research Platform — End-to-End Cycle Test")
    print("  Survey → Planning → Analysis → (Survey#2?)")
    print("=" * 60)
    print(Colors.END)

    if mock_mode:
        # Install mock before running (imports happen inside run_test)
        # We need to defer mock install to after imports, so pass a flag
        pass

    asyncio.run(run_test(skip_survey=skip_survey, dry_run=dry_run, mock=mock_mode))
