"""Throwaway diagnostic: load the real app module with a stubbed Streamlit,
build the actual grounded prompt for the two-defect query, and run the 7B."""
import sys
import types
import importlib.util
import contextlib


# ---- Minimal Streamlit stub so app.py imports & its module-level code runs ----
class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _SessionState(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)

    def __setattr__(self, k, v):
        self[k] = v


def _noop(*a, **k):
    return None


def _cache_resource(*d_args, **d_kwargs):
    # Support both @st.cache_resource and @st.cache_resource(show_spinner=...)
    if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
        return d_args[0]

    def wrap(fn):
        return fn

    return wrap


def _fragment(*d_args, **d_kwargs):
    if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
        return d_args[0]

    def wrap(fn):
        return fn

    return wrap


def _columns(spec, **k):
    n = spec if isinstance(spec, int) else len(spec)
    cols = []
    for _ in range(n):
        c = _Ctx()
        c.metric = _noop
        c.markdown = _noop
        c.button = lambda *a, **k: False
        cols.append(c)
    return cols


st = types.ModuleType("streamlit")
st.set_page_config = _noop
st.title = _noop
st.divider = _noop
st.markdown = _noop
st.caption = _noop
st.info = _noop
st.error = _noop
st.metric = _noop
st.rerun = _noop
st.button = lambda *a, **k: False
st.chat_input = lambda *a, **k: None
st.columns = _columns
st.container = lambda *a, **k: _Ctx()
st.empty = lambda *a, **k: _Ctx()
st.chat_message = lambda *a, **k: _Ctx()
st.sidebar = _Ctx()
st.cache_resource = _cache_resource
st.fragment = _fragment
st.session_state = _SessionState(messages=[])
sys.modules["streamlit"] = st

# ---- Load the real app module ----
spec = importlib.util.spec_from_file_location("appmod", "/app/app.py")
app = importlib.util.module_from_spec(spec)
with contextlib.suppress(Exception):
    spec.loader.exec_module(app)

prompt = "tell me about 8c3a23c6 and 38a3bba6"
grounded = app.build_grounded_prompt(prompt)

print("=" * 30, "INTENT/ROUTE printed above", "=" * 30)
print("PROMPT CHARS:", len(grounded))
# Show how many DETECTION blocks and assessment blocks the model will see.
print("DETECTION RECORDS 'Detection' count:", grounded.count("--- Detection"))
print("Assessment 'Detection ' headers:", grounded.count("):\n  OUT OF RANGE"))

print("\n===== RUNNING 7B ON THE REAL PROMPT =====\n")
out = []
for tok in app.call_llm_stream(prompt):
    out.append(tok)
text = "".join(out)
print(text)
print("\n===== END =====")
print("Response mentions Spatter:", "spatter" in text.lower(),
      "| Edge Weld:", "edge weld" in text.lower())
print("Number of '### Detection' headings:", text.count("### Detection"))
