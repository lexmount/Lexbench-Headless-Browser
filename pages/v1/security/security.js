window.__kitesurfFixture = {version: "v1", fixture: "security", cspViolation: null};
addEventListener("securitypolicyviolation", (event) => {
  window.__kitesurfFixture.cspViolation = `${event.violatedDirective}:${event.blockedURI}`;
});
document.querySelector("#try-eval").onclick = () => {
  try {
    eval("1 + 1");
    document.querySelector("#security-result").textContent = "eval-allowed";
  } catch (error) {
    document.querySelector("#security-result").textContent = `eval-blocked:${error.name}`;
  }
};
