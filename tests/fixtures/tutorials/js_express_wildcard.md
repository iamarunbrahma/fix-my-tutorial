# A custom 404 page in Express

```bash
npm init -y
npm install express
```

Create `server.js`:

```js
const express = require('express');
const app = express();

app.get('/', (req, res) => res.send('home'));
app.get('*', (req, res) => res.status(404).send('not found'));

app.listen(3000, () => console.log('listening on 3000'));
```

Start it with `node server.js` and open http://localhost:3000/.
