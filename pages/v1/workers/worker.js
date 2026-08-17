self.onmessage = (event) => self.postMessage(`dedicated:${event.data * 2}`);
