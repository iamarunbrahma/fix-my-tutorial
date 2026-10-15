# Hashing a string with Node's crypto module

Create `hash.js`:

```js
const { createHash } = require('node:crypto');

console.log(createHash('sha256').update('hello').digest('hex').slice(0, 12));
```

Run it with `node hash.js`. You should see `2cf24dba5fb0`.
