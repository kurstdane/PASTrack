from __future__ import annotations

# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportIncompatibleVariableOverride=false

import secrets
import string
import uuid
from typing import ClassVar
from pathlib import PurePosixPath

from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.mail import send_mail
from django.db import models
from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify


class CustomUserManager(UserManager):
    def create_user(self, email: str, password: str | None = None, **extra_fields):
        if not email:
            raise ValueError("The Email must be set")

        extra_fields.setdefault("role", "lgu_admin")
        email = self.normalize_email(email)

        # Allow callers to pass `username` (Staff ID) without conflicting with
        # our default blank username behavior.
        username = extra_fields.pop("username", "")
        user = self.model(email=email, username=username, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str, **extra_fields):
        extra_fields.setdefault("role", "super_admin")
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("account_status", "active")
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email=email, password=password, **extra_fields)

class CustomUser(AbstractUser):
    # roles
    ROLE_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("super_admin", "Super Admin"),
        ("lgu_admin", "LGU Admin"),
        ("capitol_receiving", "Receiver"),
        ("capitol_examiner", "Examiner"),
        ("capitol_approver", "Approver"),
        ("capitol_taxmapper", "Tax Mapper"),
        ("capitol_numberer", "Numberer"),
        ("capitol_releaser", "Releaser"),
    ]

    email = models.EmailField(unique=True, blank=False, null=False)
    middle_initial = models.CharField(max_length=10, blank=True)
    suffix = models.CharField(max_length=20, blank=True)
    full_name = models.CharField(max_length=255, blank=True)
    designation = models.CharField(max_length=120, blank=True)
    position = models.CharField(max_length=120, blank=True)
    role = models.CharField(max_length=50, choices=ROLE_CHOICES, null=False)

    photo = models.ImageField(upload_to="profile_photos/", blank=True, null=True)

    THEME_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("light", "Light"),
        ("dark", "Dark"),
        ("system", "System"),
    ]
    theme_preference = models.CharField(max_length=16, choices=THEME_CHOICES, default="light")
    timezone_preference = models.CharField(max_length=64, default="Asia/Manila")
    date_format_preference = models.CharField(max_length=32, default="YYYY-MM-DD")

    notify_new_account_activations = models.BooleanField(default=True)
    notify_weekly_activity_report = models.BooleanField(default=False)
    notify_critical_system_alerts = models.BooleanField(default=True)

    # Module 1.2: force password change on first login for admin-created accounts
    must_change_password = models.BooleanField(default=False)

    ACCOUNT_STATUS_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("pending", "Pending Activation"),
        ("active", "Active"),
        ("inactive", "Inactive"),
    ]

    LGU_MUNICIPALITY_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("Alcantara", "Alcantara"),
        ("Alcoy", "Alcoy"),
        ("Alegria", "Alegria"),
        ("Aloguinsan", "Aloguinsan"),
        ("Argao", "Argao"),
        ("Asturias", "Asturias"),
        ("Badian", "Badian"),
        ("Balamban", "Balamban"),
        ("Bantayan", "Bantayan"),
        ("Barili", "Barili"),
        ("Boljoon", "Boljoon"),
        ("Borbon", "Borbon"),
        ("Carmen", "Carmen"),
        ("Catmon", "Catmon"),
        ("Compostela", "Compostela"),
        ("Consolacion", "Consolacion"),
        ("Cordova", "Cordova"),
        ("Daanbantayan", "Daanbantayan"),
        ("Dalaguete", "Dalaguete"),
        ("Dumanjug", "Dumanjug"),
        ("Ginatilan", "Ginatilan"),
        ("Liloan", "Liloan"),
        ("Madridejos", "Madridejos"),
        ("Malabuyoc", "Malabuyoc"),
        ("Medellin", "Medellin"),
        ("Minglanilla", "Minglanilla"),
        ("Moalboal", "Moalboal"),
        ("Oslob", "Oslob"),
        ("Pilar (Camotes)", "Pilar (Camotes)"),
        ("Pinamungajan", "Pinamungajan"),
        ("Poro (Camotes)", "Poro (Camotes)"),
        ("Ronda", "Ronda"),
        ("Samboan", "Samboan"),
        ("San Fernando", "San Fernando"),
        ("San Francisco (Camotes)", "San Francisco (Camotes)"),
        ("San Remigio", "San Remigio"),
        ("Santa Fe (Bantayan Island)", "Santa Fe (Bantayan Island)"),
        ("Santander", "Santander"),
        ("Sibonga", "Sibonga"),
        ("Sogod", "Sogod"),
        ("Tabogon", "Tabogon"),
        ("Tabuelan", "Tabuelan"),
        ("Tuburan", "Tuburan"),
        ("Tudela (Camotes)", "Tudela (Camotes)"),
    ]
    account_status = models.CharField(max_length=20, choices=ACCOUNT_STATUS_CHOICES, default="pending")
    activation_nonce = models.CharField(max_length=64, blank=True, default="")
    activation_sent_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    temp_password_created_at = models.DateTimeField(null=True, blank=True)

    lgu_municipality = models.CharField(
        max_length=64,
        blank=True,
        choices=LGU_MUNICIPALITY_CHOICES,
        default="",
    )

    # Security (Module 1): account lockout after consecutive failed logins
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    lockout_until = models.DateTimeField(null=True, blank=True)

    # ---------- Password Recovery (Module 1.3) ----------
    password_reset_code = models.CharField(max_length=6, blank=True, default="")
    password_reset_code_created_at = models.DateTimeField(null=True, blank=True)
    password_change_count_this_month = models.PositiveSmallIntegerField(default=0)
    last_password_change_at = models.DateTimeField(null=True, blank=True)

    # Use email for login instead of username
    USERNAME_FIELD: ClassVar[str] = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects = CustomUserManager()

    def __str__(self):
        return f"{self.full_name} ({self.email}) - {self.get_role_display()}"

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"

    def generate_staff_id(self, role_prefix):
        """Generate Staff ID: YY-CEB-0001"""
        from .models import CustomUser
        yy = timezone.localtime(timezone.now()).strftime("%y")
        prefix_map = {
            "super_admin": "ADM",
            "lgu_admin": "LGU",
            "capitol_receiving": "REC",
            "capitol_examiner": "EXM",
            "capitol_approver": "APR",
            "capitol_taxmapper": "TAX",
            "capitol_numberer": "NUM",
            "capitol_releaser": "REL",
        }
        prefix = prefix_map.get(role_prefix, "USR")
        last_user = CustomUser.objects.filter(
            role=role_prefix
        ).order_by("id").last()
        seq = (last_user.id + 1) if last_user else 1
        return f"{yy}-{prefix}-{seq:04d}"

    def generate_temp_password(self):
        return "123456"

    def issue_activation(self, *, request, temp_password: str, send_email: bool | None = None) -> str:
        """Issue a 1-hour activation link and record activation metadata.

        When email sending is disabled (common in local/dev), the activation link
        is returned so the caller can display it on-screen.

        The temp password itself expires after 7 days.
        """
        from django.core import signing
        from django.urls import reverse

        now = timezone.now()
        self.account_status = "pending"
        self.is_active = False
        self.activation_sent_at = now
        self.activation_nonce = secrets.token_urlsafe(24)
        if not self.temp_password_created_at:
            self.temp_password_created_at = now
        self.save(update_fields=["account_status", "is_active", "activation_sent_at", "activation_nonce", "temp_password_created_at"])

        token = signing.dumps(
            {"uid": self.pk, "nonce": self.activation_nonce},
            salt="core.activate",
        )
        activation_link = request.build_absolute_uri(reverse("activate_account", kwargs={"token": token}))

        if send_email is None:
            send_email = bool(getattr(settings, "LEGALTRACK_SEND_EMAILS", True))

        subject = "Welcome to PAStrack — Activate Your Account"
        message = (
            f"Welcome to PAStrack, {self.full_name or self.email}!\n\n"
            "We're pleased to have you on board.\n\n"
            "Your account has been created and is ready for activation. "
            "PAStrack is the Provincial Assessor's Office Tracking System, designed to "
            "streamline document processing and provide secure, transparent tracking "
            "from submission to final release.\n\n"
            "Your Account Details\n"
            f"Staff ID: {self.username}\n"
            f"Email Address: {self.email}\n"
            f"Temporary Password: {temp_password}\n\n"
            "Activate Your Account\n"
            "Use the secure link below to activate your account and set your personal password:\n\n"
            f"{activation_link}\n\n"
            "Security Notes\n"
            "- This activation link will expire in 1 hour.\n"
            "- You will be required to create a new strong password during activation.\n"
            "- Your temporary password remains valid for up to 7 days.\n"
            "- If the link expires or you encounter any issues, please contact the "
            "Super Administrator to request a new activation email.\n\n"
            "We look forward to having you use PAStrack to support more efficient and "
            "transparent document processing.\n\n"
            "Warm regards,\n"
            "The PAStrack Team\n"
        )

        if send_email:
            try:
                from django.core.mail import send_mail
                send_mail(
                    subject,
                    message,
                    getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@cebu.gov.ph"),
                    [self.email],
                    fail_silently=False,
                )
            except Exception as e:
                import sys
                print(f"[SMTP-ACTIVATION-ERROR] FAILED: {e}", file=sys.stderr)
                # Re-raise so the view can catch it or it shows up in Render logs clearly
                raise e

        return activation_link

    def save(self, *args, **kwargs):
        created_by = kwargs.pop("created_by", None)
        is_new = self.pk is None

        temp_password: str | None = None

        last_name = (self.last_name or "").strip()
        first_name = (self.first_name or "").strip()
        middle_initial = (self.middle_initial or "").strip()
        suffix = (self.suffix or "").strip()

        if last_name or first_name or middle_initial or suffix:
            main = ", ".join([p for p in [last_name, first_name] if p])
            rest = " ".join([p for p in [middle_initial, suffix] if p])
            self.full_name = (main + (" " + rest if rest else "")).strip().strip(",")

        if is_new:
            # Generate Staff ID
            self.username = self.generate_staff_id(self.role)

            # Superusers (created via `createsuperuser`) should be active immediately.
            if self.is_superuser:
                if not self.role:
                    self.role = "super_admin"
                self.account_status = "active"
                self.is_active = True
                self.must_change_password = False
            else:
                # If password was already set by the creator workflow, keep it.
                # Otherwise, generate a temp password.
                if not self.password:
                    temp_password = self.generate_temp_password()
                    self.set_password(temp_password)
                    self.must_change_password = True

                # Module 1: new accounts start in Pending Activation
                self.account_status = "pending"
                self.is_active = False
                self.temp_password_created_at = self.temp_password_created_at or timezone.now()

        # Keep is_active consistent with account_status when not pending.
        if self.account_status == "active":
            self.is_active = True
        elif self.account_status in {"pending", "inactive"}:
            self.is_active = False

        super().save(*args, **kwargs)

        if is_new:
            # Optional: Log password in console for dev
            if settings.DEBUG and temp_password:
                print("\n=== NEW USER CREATED ===")
                print(f"Email: {self.email}")
                print(f"Staff ID: {self.username}")
                print(f"Password: {temp_password}")
                print("Login: http://127.0.0.1:8000/login/")
                print("========================\n")

            # Audit log
            AuditLog.objects.create(
                actor=created_by,
                action="create_user",
                target_user=self,
                target_object=f"User: {self.email}",
                details={
                    "staff_id": self.username,
                    "role": self.get_role_display(),
                    "account_status": self.account_status,
                }
            )

# Base model for audit trails and timestamps
class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_created"
    )

    class Meta:
        abstract = True

class AuditLog(TimestampedModel):
    ACTION_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("login", "User Login"),
        ("login_failed", "User Login Failed"),
        ("logout", "User Logout"),
        ("create_user", "Create User Account"),
        ("update_user", "Update User Account"),
        ("deactivate_user", "Deactivate User"),
        ("reactivate_user", "Reactivate User"),
        ("reset_password", "Reset Password"),
        ("activation_email_sent", "Activation Email Sent"),
        ("activate_account", "Account Activated"),
        ("password_reset_request", "Password Reset Requested"),
        ("password_reset_complete", "Password Reset Completed"),
        ("case_create", "Transaction Created"),
        ("case_update", "Transaction Updated"),
        ("case_document_review", "Transaction Document Reviewed"),
        ("case_remark", "Transaction Remark Added"),
        ("case_status_change", "Transaction Status Changed"),
        ("case_receipt", "Transaction Physically Received"),
        ("case_assignment", "Transaction Assigned"),
        ("case_approval", "Transaction Approved"),
        ("case_rejection", "Transaction Rejected"),
        ("case_numbered", "Transaction Number Assigned"),
        ("case_release", "Transaction Released"),
        ("support_feedback", "Support Feedback Submitted"),
    ]

    actor = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_logs"
    )
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    target_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="target_audit_logs"
    )
    target_object = models.CharField(max_length=255, blank=True, help_text="e.g., Case: PAS26010001")
    details = models.JSONField(default=dict, blank=True, help_text="Extra context in JSON")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list] = [
            models.Index(fields=["action"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["actor"]),
        ]
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"

    def __str__(self):
        return f"{self.get_action_display()} by {self.actor} at {self.created_at}"


class PasswordResetRequest(models.Model):
    email = models.EmailField()
    requested_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes: ClassVar[list] = [
            models.Index(fields=["email"]),
            models.Index(fields=["requested_at"]),
        ]


class LGUTaxDeclarationSequence(models.Model):
    """Tracks the current TD sequence for each LGU to ensure strict sequential numbering."""
    lgu_name = models.CharField(max_length=100, unique=True, choices=CustomUser.LGU_MUNICIPALITY_CHOICES)
    prefix = models.CharField(max_length=10, help_text="e.g., '120-' for Alcoy")
    current_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.lgu_name} ({self.prefix}) - Next: {self.current_count + 1:03d}"

    @classmethod
    def get_next_number(cls, lgu_name):
        """Safely generates the next sequential number using a database lock."""
        with transaction.atomic():
            # select_for_update() locks this specific row until the transaction finishes.
            # This completely prevents two cases from getting the same number.
            sequence, created = cls.objects.select_for_update().get_or_create(
                lgu_name=lgu_name,
                defaults={'prefix': '000-'} # You can update prefixes in the Django Admin
            )
            
            sequence.current_count += 1
            sequence.save()
            
            # Formats as 120-001, 120-002, etc. (padding with 3 zeros)
            return f"{sequence.prefix}{sequence.current_count:03d}"

class Case(TimestampedModel):
    # ---------- Tracking ID ----------
    tracking_id = models.CharField(max_length=30, unique=True, editable=False, blank=True, null=True)

    # Draft identifier (internal). Drafts do not get a tracking number until submitted.
    draft_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    # ---------- Status ----------
    # ---------- Status ----------
    STATUS_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("draft", "Draft"),
        ("not_received", "Not Received"),
        ("received", "Received"),
        ("to_examine", "To Examine"),       # Status after Receiver assigns it
        ("in_review", "Under Examination"), # Status once Examiner starts working
        ("for_taxmapping", "For Taxmapping"),
        ("for_approval", "For Approval"),
        ("approved", "Approved"),
        ("for_numbering", "For Numbering"),
        ("for_release", "For Release"),
        ("released", "Released"),
        ("client_correction", "Client Correction (30 days)"),
        ("returned", "Returned (Legacy)"),
        ("withdrawn", "Withdrawn"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="not_received")

    OWNERSHIP_TYPE_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ('single',      'Single Owner'),
        ('married',     'Married / Spouses'),
        ('corporation', 'Corporation'),
        ('others',      'Co-ownership'),
    ]

    ownership_type = models.CharField(
        max_length=20,
        choices=OWNERSHIP_TYPE_CHOICES,
        default='single',
        help_text="Ownership classification of the property owner"
    )

    # For Married / Spouses
    spouse_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Full name of spouse (if Married/Spouses)"
    )

    # For Corporation
    corporation_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Company or corporation name"
    )

    # For Others
    co_owners = models.TextField(
        blank=True,
        null=True,
        help_text="List of co-owners (one per line)"
    )
    # ---------- Client info ----------
    client_name = models.CharField(max_length=255, blank=True, default="")
    client_contact = models.CharField(max_length=100, blank=True, default="")   # phone / email

    client_first_name = models.CharField(max_length=120, blank=True, default="")
    client_last_name = models.CharField(max_length=120, blank=True, default="")
    client_middle_name = models.CharField(max_length=120, blank=True, default="")
    client_suffix = models.CharField(max_length=40, blank=True, default="")
    client_number = models.CharField(max_length=40, blank=True, default="")
    client_email = models.EmailField(blank=True, default="")

    CASE_TYPE_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("land_first_time", "Land declared for the first-time"),
        ("building_improvements", "Building and other improvements / Machineries"),
        ("subdivision_consolidation", "Subdivision or Consolidation"),
        ("reassessment_reclassification", "Re-assessment / Re-classification"),
        ("area_increase_decrease", "Increase / Decrease of Area"),
        ("transfer_ownership_tax_decl", "Transfer of Ownership of Tax Declaration"),
    ]

    case_type = models.CharField(max_length=64, choices=CASE_TYPE_CHOICES, blank=True, default="")

    PROPERTY_TITLE_TYPE_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("titled", "Titled Property"),
        ("untitled", "Untitled Property"),
    ]
    property_title_type = models.CharField(
        max_length=20,
        blank=True,
        default="",
        choices=PROPERTY_TITLE_TYPE_CHOICES,
        help_text="Required only for Land First Time and Transfer of Ownership cases.",
    )

    CLASSIFICATION_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ('residential',  'Residential'),
        ('agricultural', 'Agricultural'),
        ('commercial',   'Commercial'),
        ('industrial',   'Industrial'),
        ('mineral',      'Mineral'),
        ('timberland',   'Timberland'),
        ('special',      'Special'),
    ]
    classification = models.CharField(
        max_length=20,
        choices=CLASSIFICATION_CHOICES,
        blank=True,
        null=True,
    )

    area_value = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        help_text="Numerical area of the property (e.g. 250.0000)",
        blank=True,
        null=True,
    )

    AREA_UNIT_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ('sqm', 'Square Meters (sq.m.)'),
        ('ha',  'Hectares (ha.)'),
    ]
    area_unit = models.CharField(
        max_length=10,
        choices=AREA_UNIT_CHOICES,
        default='sqm',
        help_text="Unit of measurement for the property area",
        blank=True,
        null=True,
    )

    # ---------- LGU who submitted ----------
    submitted_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="submitted_cases"
    )

    # LGU metadata
    lgu_area_code = models.CharField(max_length=12, blank=True, default="")
    area = models.CharField(
        max_length=64,
        blank=True,
        choices=CustomUser.LGU_MUNICIPALITY_CHOICES,
        default="",
    )

    # ---------- Checklist (JSON) ----------
    checklist = models.JSONField(
        default=list,
        help_text="List of dicts: [{'doc_type': 'Land Title', 'required': True, 'uploaded': False}]"
    )

    # ---------- Timestamps ----------
    received_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_cases"
    )
    received_at = models.DateTimeField(null=True, blank=True)

    returned_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returned_cases"
    )
    returned_at = models.DateTimeField(null=True, blank=True)
    return_reason = models.TextField(blank=True)
    client_correction_deadline = models.DateTimeField(null=True, blank=True)

    # ---------- Assignment ----------
    assigned_to = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_cases"
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

    # ---------- Tax Mapping ----------
    needs_taxmapping = models.BooleanField(default=False)
    taxmapper_assigned_to = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="taxmapped_cases"
    )
    taxmapped_at = models.DateTimeField(null=True, blank=True)

    released_at = models.DateTimeField(null=True, blank=True)
    lgu_submitted_at = models.DateTimeField(null=True, blank=True)
    
    # ---------- Release Info ----------
    claimed_by_name = models.CharField(max_length=120, blank=True, default="")
    claimed_by_contact = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["updated_at"]),
        ]
        verbose_name = "Transaction"
        verbose_name_plural = "Transactions"

    def __str__(self):
        if self.tracking_id:
            return f"{self.tracking_id} - {self.client_name}"
        return f"Draft {self.draft_id} - {self.client_name}"
    @property
    def co_owners_list(self) -> list[str]:
        if self.co_owners:
            return [name.strip() for name in self.co_owners.split(',') if name.strip()]
        return []

    @property
    def client_display_name(self) -> str:
        last_name = (self.client_last_name or "").strip()
        first_name = (self.client_first_name or "").strip()
        middle_name = (self.client_middle_name or "").strip()
        suffix = (self.client_suffix or "").strip()

        if last_name or first_name or middle_name or suffix:
            # Preferred display: Last, First Middle Suffix
            main = ", ".join([p for p in [last_name, first_name] if p])
            rest = " ".join([p for p in [middle_name, suffix] if p])
            return (main + (" " + rest if rest else "")).strip().strip(",")

        return (self.client_name or "").strip()

    @property
    def client_display_contact(self) -> str:
        email = (self.client_email or "").strip()
        number = (self.client_number or "").strip()
        
        # Format number with +63 for display/audit purposes only
        # The stored number is just the 10 digits (9XXXXXXXXX)
        formatted_number = f"+63{number}" if number else ""
        
        if email and formatted_number:
            return f"{formatted_number} / {email}"
        return formatted_number or email or (self.client_contact or "").strip()

    # ------------------------------------------------------------------
    # Auto-generate tracking_id: PAS[YY][RANDOM]
    # - Example: PAS26A7S12B
    # ------------------------------------------------------------------
    def generate_tracking_id(self) -> str:
        now = timezone.localtime(timezone.now())
        yy = now.strftime("%y")
        
        prefix = "PAS"
        if self.submitted_by and self.submitted_by.role == "lgu_admin" and self.area:
            lgu_code = self.area[:3].upper()
            prefix = f"LGU{lgu_code}-PAS"
            
        # PAS + YY + 6 random alphanumeric characters
        chars = string.ascii_uppercase + string.digits
        
        for _ in range(10):
            random_part = "".join(secrets.choice(chars) for _ in range(6))
            tid = f"{prefix}{yy}{random_part}"
            if not Case.objects.filter(tracking_id=tid).exists():
                return tid
        
        raise ValueError("Unable to generate unique tracking_id")


    td_number = models.CharField(
        max_length=50, 
        blank=True, 
        null=True, 
        unique=True, 
        help_text="Official Tax Declaration Number assigned by Numberer"
    )
    # ------------------------------------------------------------------
    #  Save override
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        # Keep legacy `client_name` / `client_contact` populated for existing pages/reports.
        # Prefer explicit client_* fields when present.
        if not (self.client_name or "").strip():
            display = (self.client_display_name or "").strip()
            if display:
                self.client_name = display

        if not (self.client_contact or "").strip():
            contact = (self.client_display_contact or "").strip()
            if contact:
                self.client_contact = contact

        if self.tracking_id:
            if self.tracking_id.startswith("PAS") or self.tracking_id.startswith("LGU"):
                return super().save(*args, **kwargs)
            else:
                # Force regeneration if the prefix is wrong
                self.tracking_id = ""

        # Drafts (not yet submitted) intentionally have no tracking_id.
        if self.lgu_submitted_at is None:
            return super().save(*args, **kwargs)

        if not self.tracking_id:
            self.tracking_id = self.generate_tracking_id()
            
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                fields = list(kwargs["update_fields"])
                if "tracking_id" not in fields:
                    fields.append("tracking_id")
                kwargs["update_fields"] = fields

        last_err: Exception | None = None
        for _ in range(10):
            if not self.tracking_id:
                self.tracking_id = self.generate_tracking_id()

            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                fields = list(kwargs["update_fields"])
                if "tracking_id" not in fields:
                    fields.append("tracking_id")
                kwargs["update_fields"] = fields

            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                last_err = exc
                self.tracking_id = ""
                continue

        raise last_err or IntegrityError("Unable to generate unique tracking_id")


def case_document_upload_to(instance, filename: str) -> str:
    def _safe_filename(name: str, *, max_len: int = 120) -> str:
        # Browsers sometimes send a fake path (e.g., C:\\fakepath\\file.pdf).
        # Normalize to a basename and truncate to avoid FileField max_length issues.
        raw = (name or "").replace("\\", "/")
        base = PurePosixPath(raw).name
        if not base:
            return "upload"

        # Keep extension if present.
        if "." in base:
            stem, _, ext = base.rpartition(".")
            ext = ext.lower()
            ext_part = f".{ext}" if ext else ""
            stem = stem or "file"
        else:
            stem, ext_part = base, ""

        # Strip odd whitespace; keep characters as-is (storage will handle).
        stem = " ".join(stem.split()).strip() or "file"

        # Ensure total <= max_len
        allowed = max(1, max_len - len(ext_part))
        if len(stem) > allowed:
            stem = stem[:allowed]
        return f"{stem}{ext_part}"

    case = getattr(instance, "case", None)
    tracking = getattr(case, "tracking_id", None)
    draft_id = getattr(case, "draft_id", None)
    key = tracking or (str(draft_id) if draft_id else "unknown")
    doc_type = (slugify(getattr(instance, "doc_type", "") or "document") or "document")[:60]
    safe_name = _safe_filename(str(filename))
    return f"cases/{key}/{doc_type}/{safe_name}"


def archived_case_document_upload_to(instance, filename: str) -> str:
    from pathlib import PurePosixPath
    from django.utils.text import slugify

    def _safe_filename(name: str, *, max_len: int = 120) -> str:
        raw = (name or "").replace("\\", "/")
        base = PurePosixPath(raw).name
        if not base:
            return "upload"

        if "." in base:
            stem, _, ext = base.rpartition(".")
            ext = ext.lower()
            ext_part = f".{ext}" if ext else ""
            stem = stem or "file"
        else:
            stem, ext_part = base, ""

        stem = " ".join(stem.split()).strip() or "file"
        allowed = max(1, max_len - len(ext_part))
        if len(stem) > allowed:
            stem = stem[:allowed]
        return f"{stem}{ext_part}"

    case = getattr(instance, "case", None)
    tracking = getattr(case, "tracking_id", None)
    draft_id = getattr(case, "draft_id", None)
    key = tracking or (str(draft_id) if draft_id else "unknown")
    doc_type = (slugify(getattr(instance, "doc_type", "") or "document") or "document")[:60]
    safe_name = _safe_filename(str(filename))
    return f"cases/{key}/{doc_type}/archive/{safe_name}"


class CaseDocument(TimestampedModel):
    case = models.ForeignKey("Case", on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=120)
    file = models.FileField(upload_to=case_document_upload_to, max_length=1024)
    reviewed_ok = models.BooleanField(default=False)
    review_remark = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(
        "CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_case_documents",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        "CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_case_documents",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-uploaded_at"]
        constraints: ClassVar[list] = [
            models.UniqueConstraint(fields=["case", "doc_type"], name="uniq_case_doc_type"),
        ]

    def __str__(self):
        key = self.case.tracking_id or str(getattr(self.case, "draft_id", ""))
        return f"{key} - {self.doc_type}"


class DocumentVersion(TimestampedModel):
    case = models.ForeignKey("Case", on_delete=models.CASCADE, related_name="document_versions")
    doc_type = models.CharField(max_length=120)
    file = models.FileField(upload_to=case_document_upload_to, max_length=1024)
    uploaded_by = models.ForeignKey(
        "CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_document_versions",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-uploaded_at"]

    def __str__(self):
        key = self.case.tracking_id or str(getattr(self.case, "draft_id", ""))
        return f"{key} - {self.doc_type} (Version)"


class ArchivedCaseDocument(models.Model):
    case = models.ForeignKey("Case", on_delete=models.CASCADE, related_name="archived_documents")
    doc_type = models.CharField(max_length=120)
    file = models.FileField(upload_to=archived_case_document_upload_to, max_length=1024)
    original_filename = models.CharField(max_length=255, blank=True, default="")
    archived_by = models.ForeignKey(
        "CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_case_documents",
    )
    archived_at = models.DateTimeField(auto_now_add=True)
    keep_until = models.DateTimeField()

    class Meta:
        ordering: ClassVar[list[str]] = ["-archived_at"]
        indexes: ClassVar[list] = [
            models.Index(fields=["case"]),
            models.Index(fields=["keep_until"]),
        ]

    def __str__(self):
        key = self.case.tracking_id or str(getattr(self.case, "draft_id", ""))
        return f"{key} - {self.doc_type} (archived)"


class CaseNumber(TimestampedModel):
    case = models.ForeignKey("Case", on_delete=models.CASCADE, related_name="numbers")
    number = models.CharField(max_length=5, unique=True, blank=False, default="")

    class Meta:
        ordering: ClassVar[list[str]] = ["number"]
        indexes: ClassVar[list] = [
            models.Index(fields=["number"]),
            models.Index(fields=["case"]),
        ]

    def __str__(self):
        key = self.case.tracking_id or str(getattr(self.case, "draft_id", ""))
        return f"{key} - {self.number}"

    def save(self, *args, **kwargs):
        raw = (self.number or "").strip()
        if raw.isdigit():
            if len(raw) > 5:
                raise ValueError("Number must be at most 5 digits.")
            self.number = raw.zfill(5)
        super().save(*args, **kwargs)


class CaseRemark(TimestampedModel):
    case = models.ForeignKey("Case", on_delete=models.CASCADE, related_name="remarks")
    text = models.TextField()

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self):
        author = getattr(self.created_by, "email", "") if self.created_by else ""
        key = self.case.tracking_id or str(getattr(self.case, "draft_id", ""))
        return f"Remark on {key} by {author}"


class FAQItem(TimestampedModel):
    question = models.CharField(max_length=255)
    answer = models.TextField()
    is_published = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering: ClassVar[list[str]] = ["sort_order", "id"]

    def __str__(self):
        return self.question


class SupportFeedback(TimestampedModel):
    name = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    message = models.TextField()
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self):
        return f"Feedback {self.id} ({'resolved' if self.resolved else 'open'})"
