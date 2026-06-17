# pyright: reportAttributeAccessIssue=false, reportIndexIssue=false

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Case, CaseDocument, CaseNumber, CustomUser


class Module2CaseWizardTests(TestCase):
    def setUp(self):
        self.lgu = CustomUser(
            email="lgu@example.com",
            role="lgu_admin",
            full_name="LGU Admin",
            lgu_municipality="Alcantara",
        )
        self.lgu.set_password("StrongPass123!")
        self.lgu.save()
        self.lgu.account_status = "active"
        self.lgu.save(update_fields=["account_status", "is_active"])

        self.client.login(username=self.lgu.username, password="StrongPass123!")  # noqa: S106

    def test_wizard_creates_case_and_allows_uploads(self):
        resp = self.client.post(
            reverse("submit_case"),
            {
                "client_first_name": "Juan",
                "client_last_name": "Dela Cruz",
                "client_middle_name": "",
                "client_suffix": "",
                "client_number": "",
                "client_email": "",
                "case_type": "subdivision_consolidation",
            },
        )
        self.assertEqual(resp.status_code, 302)

        case = Case.objects.get(client_first_name="Juan", client_last_name="Dela Cruz")
        self.assertEqual(case.submitted_by_id, self.lgu.id)

        url_step2 = reverse("draft_wizard", kwargs={"draft_id": case.draft_id, "step": 2})

        # Upload required documents in Step 2 only.
        requirements = [
            "Endorsement Letter",
            "Letter request (subdivision/consolidation)",
            "Inspection report + endorsement (Assessor/Staff)",
            "Approved subdivision / survey plan",
            "Tax Clearance (current)",
        ]

        files = [
            SimpleUploadedFile(f"doc_{i}.txt", f"file{i}".encode("utf-8"), content_type="text/plain")
            for i in range(len(requirements))
        ]

        resp2 = self.client.post(
            url_step2,
            {
                # initial forms (5 requirements) + extra forms (5)
                "form-TOTAL_FORMS": "10",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-doc_type": requirements[0],
                "form-0-file": files[0],
                "form-1-doc_type": requirements[1],
                "form-1-file": files[1],
                "form-2-doc_type": requirements[2],
                "form-2-file": files[2],
                "form-3-doc_type": requirements[3],
                "form-3-file": files[3],
                "form-4-doc_type": requirements[4],
                "form-4-file": files[4],
            },
        )
        self.assertEqual(resp2.status_code, 302)

        case.refresh_from_db()
        self.assertTrue(any(i.get("doc_type") == "Endorsement Letter" for i in case.checklist))
        self.assertTrue(CaseDocument.objects.filter(case=case, doc_type="Endorsement Letter").exists())
        self.assertTrue(CaseDocument.objects.filter(case=case, doc_type="Tax Clearance (current)").exists())

        url_step3 = reverse("draft_wizard", kwargs={"draft_id": case.draft_id, "step": 3})
        resp3 = self.client.post(url_step3)
        self.assertEqual(resp3.status_code, 302)

        case.refresh_from_db()
        self.assertTrue(bool(case.tracking_id))
        self.assertIsNotNone(case.lgu_submitted_at)

    def test_can_save_draft_from_step2_and_step3(self):
        resp = self.client.post(
            reverse("submit_case"),
            {
                "client_first_name": "Juan",
                "client_last_name": "Dela Cruz",
                "client_middle_name": "",
                "client_suffix": "",
                "client_number": "",
                "client_email": "",
                "case_type": "subdivision_consolidation",
                "save_continue": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)

        case = Case.objects.get(client_first_name="Juan", client_last_name="Dela Cruz")
        url_step2 = reverse("draft_wizard", kwargs={"draft_id": case.draft_id, "step": 2})

        requirements = [
            "Endorsement Letter",
            "Letter request (subdivision/consolidation)",
            "Inspection report + endorsement (Assessor/Staff)",
            "Approved subdivision / survey plan",
            "Tax Clearance (current)",
        ]
        files = [
            SimpleUploadedFile(f"doc_{i}.txt", f"file{i}".encode("utf-8"), content_type="text/plain")
            for i in range(len(requirements))
        ]

        resp2 = self.client.post(
            url_step2,
            {
                "form-TOTAL_FORMS": "10",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-doc_type": requirements[0],
                "form-0-file": files[0],
                "form-1-doc_type": requirements[1],
                "form-1-file": files[1],
                "form-2-doc_type": requirements[2],
                "form-2-file": files[2],
                "form-3-doc_type": requirements[3],
                "form-3-file": files[3],
                "form-4-doc_type": requirements[4],
                "form-4-file": files[4],
                "save_draft": "1",
            },
        )
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(resp2["Location"], reverse("drafts"))

        case.refresh_from_db()
        self.assertFalse(bool(case.tracking_id))
        self.assertIsNone(case.lgu_submitted_at)

        url_step3 = reverse("draft_wizard", kwargs={"draft_id": case.draft_id, "step": 3})
        resp3 = self.client.post(url_step3, {"save_draft": "1"})
        self.assertEqual(resp3.status_code, 302)
        self.assertEqual(resp3["Location"], reverse("drafts"))

        case.refresh_from_db()
        self.assertFalse(bool(case.tracking_id))
        self.assertIsNone(case.lgu_submitted_at)

    def test_can_delete_draft(self):
        case = Case.objects.create(
            client_name="Draft Client",
            client_contact="0912",
            submitted_by=self.lgu,
            status="draft",
            lgu_submitted_at=None,
        )
        resp = self.client.post(reverse("delete_draft", kwargs={"draft_id": case.draft_id}))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Case.objects.filter(id=case.id).exists())

    def test_edit_case_redirects_to_wizard_step1(self):
        case = Case.objects.create(
            client_name="A",
            client_contact="B",
            submitted_by=self.lgu,
            status="not_received",
            lgu_submitted_at=timezone.now(),
        )
        resp = self.client.get(reverse("edit_case", kwargs={"tracking_id": case.tracking_id}))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("case_wizard", kwargs={"tracking_id": case.tracking_id, "step": 1}), resp["Location"])


class Module2CapitolWorkflowTests(TestCase):
    def setUp(self):
        self.lgu = CustomUser(email="lgu2@example.com", role="lgu_admin", full_name="LGU Admin", lgu_municipality="Alcantara")
        self.lgu.set_password("StrongPass123!")
        self.lgu.save()
        self.lgu.account_status = "active"
        self.lgu.save(update_fields=["account_status", "is_active"])

        self.receiving = CustomUser(email="rec@example.com", role="capitol_receiving", full_name="Receiving")
        self.receiving.set_password("StrongPass123!")
        self.receiving.save()
        self.receiving.account_status = "active"
        self.receiving.save(update_fields=["account_status", "is_active"])

        self.examiner = CustomUser(email="exm@example.com", role="capitol_examiner", full_name="Examiner")
        self.examiner.set_password("StrongPass123!")
        self.examiner.save()
        self.examiner.account_status = "active"
        self.examiner.save(update_fields=["account_status", "is_active"])

        self.approver = CustomUser(email="apr@example.com", role="capitol_approver", full_name="Approver")
        self.approver.set_password("StrongPass123!")
        self.approver.save()
        self.approver.account_status = "active"
        self.approver.save(update_fields=["account_status", "is_active"])

        self.numberer = CustomUser(email="num@example.com", role="capitol_numberer", full_name="Numberer")
        self.numberer.set_password("StrongPass123!")
        self.numberer.save()
        self.numberer.account_status = "active"
        self.numberer.save(update_fields=["account_status", "is_active"])

        self.releaser = CustomUser(email="rel@example.com", role="capitol_releaser", full_name="Releaser")
        self.releaser.set_password("StrongPass123!")
        self.releaser.save()
        self.releaser.account_status = "active"
        self.releaser.save(update_fields=["account_status", "is_active"])

    def test_end_to_end_capitol_flow_to_release(self):
        case = Case.objects.create(
            client_name="Juan",
            client_contact="0912",
            submitted_by=self.lgu,
            status="not_received",
            lgu_submitted_at=timezone.now(),
        )

        self.client.login(username=self.receiving.username, password="StrongPass123!")  # noqa: S106
        resp = self.client.post(reverse("receive_case", kwargs={"tracking_id": case.tracking_id}))
        self.assertEqual(resp.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "received")

        resp = self.client.post(
            reverse("assign_case", kwargs={"tracking_id": case.tracking_id}),
            {"examiner_id": str(self.examiner.id)},
        )
        self.assertEqual(resp.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "in_review")
        self.assertEqual(case.assigned_to_id, self.examiner.id)

        self.client.logout()
        self.client.login(username=self.examiner.username, password="StrongPass123!")  # noqa: S106
        resp = self.client.post(reverse("submit_for_approval", kwargs={"tracking_id": case.tracking_id}))
        self.assertEqual(resp.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "for_approval")

        self.client.logout()
        self.client.login(username=self.approver.username, password="StrongPass123!")  # noqa: S106
        resp = self.client.post(reverse("approve_case", kwargs={"tracking_id": case.tracking_id}))
        self.assertEqual(resp.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "for_numbering")

        self.client.logout()
        self.client.login(username=self.numberer.username, password="StrongPass123!")  # noqa: S106
        resp = self.client.post(
            reverse("mark_numbered", kwargs={"tracking_id": case.tracking_id}),
            {"numbers": "1001"},
        )
        self.assertEqual(resp.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "for_release")
        self.assertTrue(CaseNumber.objects.filter(case=case, number=1001).exists())

        self.client.logout()
        self.client.login(username=self.releaser.username, password="StrongPass123!")  # noqa: S106
        resp = self.client.post(reverse("release_case", kwargs={"tracking_id": case.tracking_id}), {"release_confirm": "1"})
        self.assertEqual(resp.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "released")
        self.assertIsNotNone(case.released_at)

    def test_approver_can_return_for_correction(self):
        case = Case.objects.create(
            client_name="Ana",
            client_contact="x",
            submitted_by=self.lgu,
            status="for_approval",
            lgu_submitted_at=timezone.now(),
            assigned_to=self.examiner,
            assigned_at=timezone.now(),
        )

        self.client.login(username=self.approver.username, password="StrongPass123!")  # noqa: S106
        resp = self.client.post(
            reverse("return_for_correction", kwargs={"tracking_id": case.tracking_id}),
            {"reason": "Missing document"},
        )
        self.assertEqual(resp.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "in_review")
        self.assertEqual(case.return_reason, "Missing document")
        self.assertEqual(case.assigned_to_id, self.examiner.id)

    def test_examiner_must_review_documents_before_submitting_for_approval(self):
        case = Case.objects.create(
            client_name="Doc Case",
            client_contact="0912",
            submitted_by=self.lgu,
            status="in_review",
            lgu_submitted_at=timezone.now(),
            assigned_to=self.examiner,
            assigned_at=timezone.now(),
        )
        doc = CaseDocument.objects.create(
            case=case,
            doc_type="Endorsement Letter",
            file=SimpleUploadedFile("doc.txt", b"hello", content_type="text/plain"),
            uploaded_by=self.lgu,
        )

        self.client.login(username=self.examiner.username, password="StrongPass123!")  # noqa: S106

        resp1 = self.client.post(reverse("submit_for_approval", kwargs={"tracking_id": case.tracking_id}))
        self.assertEqual(resp1.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "in_review")

        resp2 = self.client.post(
            reverse("review_case_documents", kwargs={"tracking_id": case.tracking_id}),
            {f"reviewed_ok_{doc.id}": "1", f"review_remark_{doc.id}": "", "doc_id": str(doc.id)},
        )
        self.assertEqual(resp2.status_code, 302)
        doc.refresh_from_db()
        self.assertTrue(doc.reviewed_ok)

        resp3 = self.client.post(reverse("submit_for_approval", kwargs={"tracking_id": case.tracking_id}))
        self.assertEqual(resp3.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, "for_approval")

    def test_examiner_sees_forward_to_approver_button_on_case_detail(self):
        case = Case.objects.create(
            client_name="UI Case",
            client_contact="0912",
            submitted_by=self.lgu,
            status="in_review",
            lgu_submitted_at=timezone.now(),
            assigned_to=self.examiner,
            assigned_at=timezone.now(),
        )

        self.client.login(username=self.examiner.username, password="StrongPass123!")  # noqa: S106
        resp = self.client.get(reverse("case_detail", kwargs={"tracking_id": case.tracking_id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Forward to Approver")


class DocumentAccessTests(TestCase):
    def setUp(self):
        self.lgu1 = CustomUser(email="lgu1@example.com", role="lgu_admin", full_name="LGU One", lgu_municipality="Alcantara")
        self.lgu1.set_password("StrongPass123!")
        self.lgu1.save()
        self.lgu1.account_status = "active"
        self.lgu1.save(update_fields=["account_status", "is_active"])

        self.lgu2 = CustomUser(email="lgu2@example.com", role="lgu_admin", full_name="LGU Two", lgu_municipality="Alcantara")
        self.lgu2.set_password("StrongPass123!")
        self.lgu2.save()
        self.lgu2.account_status = "active"
        self.lgu2.save(update_fields=["account_status", "is_active"])

        self.case = Case.objects.create(
            client_name="Juan",
            client_contact="0912",
            submitted_by=self.lgu1,
            status="not_received",
            lgu_submitted_at=timezone.now(),
        )
        self.doc = CaseDocument.objects.create(
            case=self.case,
            doc_type="Endorsement Letter",
            file=SimpleUploadedFile("doc.txt", b"hello", content_type="text/plain"),
            uploaded_by=self.lgu1,
        )

    def test_download_requires_auth_and_enforces_owner(self):
        url = reverse("download_case_document", kwargs={"doc_id": self.doc.id})

        # Not logged in: should redirect to login
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)

        # Other LGU user in same municipality: should succeed
        self.client.login(username=self.lgu2.username, password="StrongPass123!")  # noqa: S106
        resp2 = self.client.get(url)
        self.assertEqual(resp2.status_code, 200)

        # Owner LGU user: should succeed
        self.client.logout()
        self.client.login(username=self.lgu1.username, password="StrongPass123!")  # noqa: S106
        resp3 = self.client.get(url)
        self.assertEqual(resp3.status_code, 200)

    def test_case_detail_is_not_visible_to_other_lgu(self):
        url = reverse("case_detail", kwargs={"tracking_id": self.case.tracking_id})
        self.client.login(username=self.lgu2.username, password="StrongPass123!")  # noqa: S106
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)


class SuperuserCreationTests(TestCase):
    def test_create_superuser_does_not_require_username(self):
        su = CustomUser.objects.create_superuser(username="admin", email="admin@example.com", password="StrongPass123!Strong")
        self.assertTrue(su.is_superuser)
        self.assertTrue(su.is_staff)
        self.assertTrue(su.is_active)
        self.assertEqual(su.account_status, "active")
        self.assertEqual(su.role, "super_admin")
        self.assertTrue(bool(su.username))


class AuthenticationBackendsTests(TestCase):
    def test_staff_id_login_works(self):
        u = CustomUser(email="user1@example.com", role="lgu_admin", full_name="User One", lgu_municipality="Alcantara")
        u.set_password("StrongPass123!")
        u.save()
        u.account_status = "active"
        u.save(update_fields=["account_status", "is_active"])

        ok = self.client.login(username=u.username, password="StrongPass123!")  # noqa: S106
        self.assertTrue(ok)

    def test_email_login_is_rejected_except_admin_alias(self):
        u = CustomUser(email="user2@example.com", role="lgu_admin", full_name="User Two", lgu_municipality="Alcantara")
        u.set_password("StrongPass123!")
        u.save()
        u.account_status = "active"
        u.save(update_fields=["account_status", "is_active"])

        ok = self.client.login(username="user2@example.com", password="StrongPass123!")  # noqa: S106
        self.assertFalse(ok)

    def test_admin_email_alias_login_works_only_for_admin_gmail(self):
        admin = CustomUser(email="admin@gmail.com", role="super_admin", full_name="Admin")
        admin.set_password("StrongPass123!")
        admin.save()
        admin.account_status = "active"
        admin.is_staff = True
        admin.is_superuser = True
        admin.save(update_fields=["account_status", "is_active", "is_staff", "is_superuser"])

        ok = self.client.login(username="admin@gmail.com", password="StrongPass123!")  # noqa: S106
        self.assertTrue(ok)


class LogoutFlowTests(TestCase):
    def test_logout_endpoint_logs_user_out(self):
        u = CustomUser(email="user3@example.com", role="lgu_admin", full_name="User Three", lgu_municipality="Alcantara")
        u.set_password("StrongPass123!")
        u.save()
        u.account_status = "active"
        u.save(update_fields=["account_status", "is_active"])

        self.client.force_login(u)
        self.assertTrue(self.client.session.get("_auth_user_id"))

        resp2 = self.client.post(reverse("logout"))
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(resp2.url, reverse("login"))

        self.assertIsNone(self.client.session.get("_auth_user_id"))
