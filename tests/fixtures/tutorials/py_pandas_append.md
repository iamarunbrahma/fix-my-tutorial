# Building a DataFrame row by row

```bash
pip install pandas
```

```python
import pandas as pd

df = pd.DataFrame(columns=["name", "score"])
for name, score in [("ana", 91), ("bo", 78)]:
    df = df.append({"name": name, "score": score}, ignore_index=True)
print(df)
```
