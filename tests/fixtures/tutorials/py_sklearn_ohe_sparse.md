# One-hot encoding with scikit-learn

```bash
pip install scikit-learn
```

```python
from sklearn.preprocessing import OneHotEncoder

enc = OneHotEncoder(sparse=False)
print(enc.fit_transform([["red"], ["blue"], ["red"]]))
```
