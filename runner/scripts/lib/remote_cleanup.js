"use strict";

// Shared fail-closed result contract for pages/targets created through a
// remote task connection. Cleanup evidence belongs to the adapter outcome so
// the runner can persist it before deciding whether another attempt is safe.

function withCleanupObservations(outcome, cleanup) {
  const observations = {
    ...(outcome.observations || {}),
    target_cleanup: cleanup,
    isolation_restored: cleanup.confirmed === true,
  };
  return { ...outcome, observations };
}

function finalizeCleanupOutcome(outcome, cleanup, label, options = {}) {
  const recorded = withCleanupObservations(outcome, cleanup);
  const requireConfirmed = options.requireConfirmed !== false;
  if (!requireConfirmed || cleanup.confirmed === true) return recorded;
  return {
    ok: false,
    status: "infra",
    error: {
      class: "script_error",
      message: `${label} target cleanup was not confirmed: ${JSON.stringify(cleanup)}`,
    },
    answer: outcome.answer,
    observations: { ...recorded.observations, primary_outcome: outcome },
    metrics: outcome.metrics || {},
  };
}

function applyCleanupContract(outcome, cleanup, label) {
  return finalizeCleanupOutcome(outcome, cleanup, label, {
    requireConfirmed: true,
  });
}

function noTargetCleanup(backend) {
  return {
    backend,
    required: false,
    confirmed: true,
    same_connection_as_task: true,
    creation_attempts: [],
    attempts: [],
  };
}

module.exports = {
  applyCleanupContract,
  finalizeCleanupOutcome,
  noTargetCleanup,
  withCleanupObservations,
};
