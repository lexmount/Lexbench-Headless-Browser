self.onconnect = (event) => {
  const port = event.ports[0];
  port.onmessage = (message) => port.postMessage(`shared:${message.data * 3}`);
  port.start();
};
