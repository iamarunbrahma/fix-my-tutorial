# Generating fake names for test data

```bash
npm init -y
npm install faker
```

Create `seed.js`:

```js
const faker = require('faker');

for (let i = 0; i < 3; i++) {
  console.log(faker.name.findName());
}
```

Run it with `node seed.js`.
