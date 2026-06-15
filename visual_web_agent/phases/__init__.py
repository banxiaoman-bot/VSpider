"""Agent loop phases split out of main.py (vspider-workflow §3 体量警戒线).

P1: perception phase (E1-E3 extend with reuse / partial SoM / AX diff).
G1: startup phase (RunContext + loop guards).
G2: planning phase (PlanningPhase — Planner / Reflector state machine).
G3: decision phase (make_vlm_decision — VLM ask wrapper).
"""
