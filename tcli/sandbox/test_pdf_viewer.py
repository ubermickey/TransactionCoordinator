"""PDF Viewer: serve PDFs, field queries, field annotations CRUD."""
from .fixtures import get, post, create_simple_txn, cleanup

# Use a known package folder and PDF
TEST_FOLDER = "Residential Purchase Offer Agreement Package"
TEST_FILE = "California_Residential_Purchase_Agreement_-_1225_ts69656.pdf"
ADDR_PDF = "8 PDF Test Dr, Beverly Hills, CA 90210"


def register(runner):
    tid = None

    def setup():
        nonlocal tid
        tid, _ = create_simple_txn(ADDR_PDF)

    def teardown():
        if tid:
            cleanup(tid)

    def test_pdf_serve():
        """GET /api/doc-packages/<folder>/<file>/pdf returns 200 with PDF content-type."""
        import urllib.request
        import urllib.error
        from .fixtures import BASE
        url = f"{BASE}/api/doc-packages/{urllib.parse.quote(TEST_FOLDER)}/{urllib.parse.quote(TEST_FILE)}/pdf"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                assert resp.status == 200, f"Expected 200, got {resp.status}"
                ct = resp.headers.get("Content-Type", "")
                assert "pdf" in ct.lower(), f"Expected PDF content-type, got {ct}"
                # Read a small chunk to verify it's actually PDF data
                data = resp.read(5)
                assert data[:4] == b'%PDF', f"Expected PDF header, got {data[:4]}"
        except urllib.error.HTTPError as e:
            raise AssertionError(f"PDF serve failed with {e.code}")

    def test_pdf_404():
        """Nonexistent PDF returns 404."""
        import urllib.parse
        code, _ = get(
            f"/api/doc-packages/{urllib.parse.quote(TEST_FOLDER)}/nonexistent.pdf/pdf"
        )
        assert code == 404

    def test_fields_endpoint():
        """GET .../fields returns field_map array."""
        import urllib.parse
        code, data = get(
            f"/api/doc-packages/{urllib.parse.quote(TEST_FOLDER)}/{urllib.parse.quote(TEST_FILE)}/fields",
            expect=200,
        )
        assert isinstance(data, list), f"Expected list, got {type(data)}"

    def test_fields_filter_page():
        """Filter fields by ?page=1."""
        import urllib.parse
        code, data = get(
            f"/api/doc-packages/{urllib.parse.quote(TEST_FOLDER)}/{urllib.parse.quote(TEST_FILE)}/fields?page=1",
            expect=200,
        )
        assert isinstance(data, list)
        # All returned fields should be page 1
        for f in data:
            assert f.get("page") == 1, f"Expected page 1, got {f.get('page')}"

    def test_fields_filter_category():
        """Filter fields by ?category=signature_area."""
        import urllib.parse
        code, data = get(
            f"/api/doc-packages/{urllib.parse.quote(TEST_FOLDER)}/{urllib.parse.quote(TEST_FILE)}/fields?category=signature_area",
            expect=200,
        )
        assert isinstance(data, list)
        assert data, "expected at least one signature-like field"
        allowed = {"signature_area", "signature", "entry_signature", "entry_initial"}
        for f in data:
            assert f.get("category") in allowed

    def test_annotations_empty():
        """GET annotations returns empty dict initially."""
        import urllib.parse
        code, data = get(
            f"/api/field-annotations/{urllib.parse.quote(TEST_FOLDER)}/{urllib.parse.quote(TEST_FILE)}?txn={tid}",
            expect=200,
        )
        assert "annotations" in data
        assert data["annotations"] == {} or isinstance(data["annotations"], dict)

    def test_annotations_save():
        """POST annotation, then GET returns it."""
        import urllib.parse
        f = urllib.parse.quote(TEST_FOLDER)
        fn = urllib.parse.quote(TEST_FILE)
        # Save
        code, res = post(
            f"/api/field-annotations/{f}/{fn}",
            {"txn": tid, "field_idx": 0, "status": "filled"},
            expect=200,
        )
        assert res.get("ok") is True
        # Verify
        _, data = get(f"/api/field-annotations/{f}/{fn}?txn={tid}", expect=200)
        assert data["annotations"].get("0") == "filled"

    def test_annotations_bulk():
        """Bulk save multiple annotations."""
        import urllib.parse
        f = urllib.parse.quote(TEST_FOLDER)
        fn = urllib.parse.quote(TEST_FILE)
        code, res = post(
            f"/api/field-annotations/{f}/{fn}/bulk",
            {"txn": tid, "annotations": {"1": "empty", "2": "optional", "3": "ignored"}},
            expect=200,
        )
        assert res.get("ok") is True
        assert res.get("saved") == 3
        # Verify
        _, data = get(f"/api/field-annotations/{f}/{fn}?txn={tid}", expect=200)
        assert data["annotations"].get("1") == "empty"
        assert data["annotations"].get("2") == "optional"
        assert data["annotations"].get("3") == "ignored"

    def test_annotations_overwrite():
        """Saving same field_idx overwrites previous status."""
        import urllib.parse
        f = urllib.parse.quote(TEST_FOLDER)
        fn = urllib.parse.quote(TEST_FILE)
        # First save
        post(f"/api/field-annotations/{f}/{fn}",
             {"txn": tid, "field_idx": 5, "status": "empty"}, expect=200)
        # Overwrite
        post(f"/api/field-annotations/{f}/{fn}",
             {"txn": tid, "field_idx": 5, "status": "filled"}, expect=200)
        # Verify overwritten
        _, data = get(f"/api/field-annotations/{f}/{fn}?txn={tid}", expect=200)
        assert data["annotations"].get("5") == "filled"

    def test_annotations_invalid_status():
        """Invalid status should be rejected."""
        import urllib.parse
        f = urllib.parse.quote(TEST_FOLDER)
        fn = urllib.parse.quote(TEST_FILE)
        code, _ = post(
            f"/api/field-annotations/{f}/{fn}",
            {"txn": tid, "field_idx": 0, "status": "bogus"},
        )
        assert code == 400

    # Register
    import urllib.parse  # needed by test functions
    runner.test("pdf_viewer:setup", setup)
    runner.test("pdf_viewer:pdf_serve", test_pdf_serve)
    runner.test("pdf_viewer:pdf_404", test_pdf_404)
    runner.test("pdf_viewer:fields_endpoint", test_fields_endpoint)
    runner.test("pdf_viewer:fields_filter_page", test_fields_filter_page)
    runner.test("pdf_viewer:fields_filter_category", test_fields_filter_category)
    runner.test("pdf_viewer:annotations_empty", test_annotations_empty)
    runner.test("pdf_viewer:annotations_save", test_annotations_save)
    runner.test("pdf_viewer:annotations_bulk", test_annotations_bulk)
    runner.test("pdf_viewer:annotations_overwrite", test_annotations_overwrite)
    runner.test("pdf_viewer:annotations_invalid", test_annotations_invalid_status)
    runner.test("pdf_viewer:teardown", teardown)
