window.__kitesurfFixture = {version: "v1", fixture: "network", requests: []};
document.querySelector("#fetch-json").onclick = async () => {
  const response = await fetch(`data.json?nonce=${Date.now()}`);
  const data = await response.json();
  window.__kitesurfFixture.requests.push({url: response.url, status: response.status});
  document.querySelector("#network-result").textContent = `${response.status}:${data.value}`;
};
document.querySelector("#fetch-missing").onclick = async () => {
  const response = await fetch(`missing.json?nonce=${Date.now()}`);
  window.__kitesurfFixture.requests.push({url: response.url, status: response.status});
  document.querySelector("#network-result").textContent = `missing:${response.status}`;
};
console.info("kitesurf-fixture-network-ready");
