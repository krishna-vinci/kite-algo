"""Pure alert-rule primitives: predicates and trigger semantics.

``backend.alerts.predicates`` evaluates conditions against observations with
explicit state in -> state out. ``backend.alerts.engine`` decides whether a
fired rule emits, honoring trigger semantics, cooldown, rearm, expiry and
session gating. Both modules are dependency-free (stdlib only) and pure.
"""
