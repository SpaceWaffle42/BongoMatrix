

def test_describe_files_styles_under_their_group():
    from PIL import Image
    from bongo.module import Module, Style
    from bongo.schema import describe

    class Flip(Style):
        group = "Digital"
        def render(self, now): return Image.new("RGB", (64, 32))

    class Plain(Style):
        def render(self, now): return Image.new("RGB", (64, 32))

    d = describe(Module(id="t", name="T", styles={"flip": Flip, "plain": Plain}))
    assert d["styles"]["flip"]["group"] == "Digital"
    assert "group" not in d["styles"]["plain"]
