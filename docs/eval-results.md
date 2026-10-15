# Evaluation results

| fixture | expected | verdict | category | rounds | seconds | cost $ | tavily | note |
|---|---|---|---|---|---|---|---|---|
| js_chalk_require | fixed | fixed | removed_api | 1 | 62 | 0.0067 | 0 |  |
| js_express_wildcard | fixed | fixed | unknown | 1 | 164 | 0.0079 | 0 |  |
| js_faker | fixed | fixed | renamed_package | 1 | 71 | 0.0086 | 3 |  |
| js_node_fetch_require | fixed | fixed | environment | 1 | 42 | 0.0125 | 2 |  |
| js_still_works | still_works | still_works |  | 0 | 7 | 0.0014 | 0 |  |
| py_flask_before_first_request | fixed | fixed | removed_api | 1 | 143 | 0.0096 | 2 |  |
| py_langchain_llms | fixed | fixed | removed_api | 1 | 156 | 0.0103 | 4 |  |
| py_load_boston | fixed | fixed | removed_api | 1 | 97 | 0.0091 | 1 |  |
| py_np_float | fixed | fixed | removed_api | 1 | 29 | 0.0049 | 3 |  |
| py_openai_v0 | fixed | fixed | removed_api | 2 | 86 | 0.0562 | 4 |  |
| py_pandas_append | fixed | fixed | removed_api | 1 | 57 | 0.0103 | 1 |  |
| py_pandas_iteritems | fixed | fixed | removed_api | 1 | 51 | 0.0063 | 1 |  |
| py_seaborn_style | fixed | fixed | tutorial_typo | 1 | 53 | 0.0049 | 2 |  |
| py_sklearn_ohe_sparse | fixed | fixed | removed_api | 1 | 90 | 0.0126 | 2 |  |
| py_still_works | still_works | still_works |  | 0 | 6 | 0.0012 | 0 |  |

**fixed 13/13 rot fixtures; 0 control(s) changed**
