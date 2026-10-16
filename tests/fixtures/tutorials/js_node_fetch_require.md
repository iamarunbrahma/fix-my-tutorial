# Fetching a web page in Node.js

```bash
npm init -y
npm install node-fetch
```

Create `index.js`:

```js
const fetch = require('node-fetch');

fetch('https://example.com')
  .then((res) => res.text())
  .then((body) => console.log(body.includes('Example Domain') ? 'fetched ok' : 'unexpected page'));
```

Run it with `node index.js`.
