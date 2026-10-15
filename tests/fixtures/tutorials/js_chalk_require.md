# Colorful console output with chalk

```bash
npm init -y
npm install chalk
```

Create `log.js`:

```js
const chalk = require('chalk');

console.log(chalk.green('Success!'));
```

Run it with `node log.js`.
