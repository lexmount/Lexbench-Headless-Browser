"use strict";

// Exact identity contract for experimental remote-CDP attempts.  The
// preflight identity is useful only as an expectation: every driver must call
// Browser.getVersion on the same WebSocket it subsequently uses for the task.

const IDENTITY_FIELDS = ["product", "protocolVersion", "revision"];

function normalizeIdentity(value) {
  const source = value && typeof value === "object" ? value : {};
  return {
    product: String(source.product || source.Browser || ""),
    protocolVersion: String(source.protocolVersion || source["Protocol-Version"] || ""),
    revision: String(source.revision || ""),
  };
}

function requireRemoteIdentity(value, label = "remote identity") {
  const expected = normalizeIdentity(value);
  const missing = IDENTITY_FIELDS.filter((field) => !expected[field]);
  if (missing.length) {
    throw new Error(`${label} is missing required field(s): ${missing.join(", ")}`);
  }
  return expected;
}

function parseExpectedRemoteIdentity(text) {
  if (!text) return null;
  let value;
  try {
    value = JSON.parse(text);
  } catch (error) {
    throw new Error(`REMOTE_CDP_IDENTITY_JSON is invalid: ${error.message}`);
  }
  return requireRemoteIdentity(value, "REMOTE_CDP_IDENTITY_JSON");
}

function compareRemoteIdentity(expected, actualValue) {
  const actual = normalizeIdentity(actualValue);
  const mismatches = IDENTITY_FIELDS.filter(
    (field) => actual[field] !== expected[field]
  );
  return {
    transport: "remote_cdp",
    expected,
    actual,
    compared_fields: [...IDENTITY_FIELDS],
    mismatches,
    verified: mismatches.length === 0,
    same_connection_as_task: true,
    reconnect_allowed: false,
  };
}

function assertRemoteIdentity(expected, actualValue) {
  const binding = compareRemoteIdentity(expected, actualValue);
  if (!binding.verified) {
    const detail = binding.mismatches
      .map(
        (field) =>
          `${field}=${JSON.stringify(binding.actual[field])} expected=${JSON.stringify(binding.expected[field])}`
      )
      .join(", ");
    const error = new Error(
      `remote identity mismatch on task connection: ${detail}`
    );
    error.binding = binding;
    throw error;
  }
  return binding;
}

module.exports = {
  IDENTITY_FIELDS,
  normalizeIdentity,
  requireRemoteIdentity,
  parseExpectedRemoteIdentity,
  compareRemoteIdentity,
  assertRemoteIdentity,
};
