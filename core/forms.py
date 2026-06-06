from __future__ import annotations

# pyright: reportAttributeAccessIssue=false, reportOperatorIssue=false

import os

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from datetime import timedelta
from typing import ClassVar
from .models import Case
from .models import CustomUser

class CaseSubmissionForm(forms.ModelForm):
    class Meta:
        model = Case
        fields: ClassVar[list[str]] = ["client_name", "client_contact", "checklist"]
        widgets: ClassVar[dict] = {
            "checklist": forms.Textarea(attrs={"rows": 6, "placeholder": (
                'Enter JSON list, e.g.\n'
                '[\n'
                '  {"doc_type": "Land Title", "required": true},\n'
                '  {"doc_type": "Tax Declaration", "required": true}\n'
                ']'
            )}),
        }
    client_name = forms.CharField(max_length=100)
    client_contact = forms.CharField(max_length=15)
    checklist = forms.JSONField()

    def clean_checklist(self):
        cleaned = self.cleaned_data or {}
        data = cleaned.get("checklist")

        if not data:
            return []

        # data is ALREADY a list/dict from JSONField
        if not isinstance(data, list):
            raise forms.ValidationError("Checklist must be a list of documents.")

        for item in data:
            if not isinstance(item, dict):
                raise forms.ValidationError("Each item must be a document object.")
            if not all(k in item for k in ["doc_type", "required"]):
                raise forms.ValidationError("Each document must have 'doc_type' and 'required'.")
            if not isinstance(item["required"], bool):
                raise forms.ValidationError("'required' must be true or false.")

        return data


class CaseDetailsForm(forms.ModelForm):
    needs_taxmapping = forms.BooleanField(
        required=False,
        label="Taxmapped?",
        help_text="Check if this transaction needs tax mapping.",
    )

    class Meta:
        model = Case
        fields: ClassVar[list[str]] = [
            "client_first_name",
            "client_last_name",
            "client_middle_name",
            "client_suffix",
            "client_number",
            "client_email",
            "area",
            "classification",
            "area_value",
            "area_unit",
            "case_type",
            "property_title_type",
            "needs_taxmapping",
        ]
        widgets: ClassVar[dict] = {
            "client_first_name": forms.TextInput(attrs={"placeholder": "First name"}),
            "client_last_name": forms.TextInput(attrs={"placeholder": "Last name"}),
            "client_middle_name": forms.TextInput(attrs={"placeholder": "Middle name"}),
            "client_suffix": forms.TextInput(attrs={"placeholder": "Suffix (optional)"}),
            "client_number": forms.TextInput(attrs={
                "placeholder": "9XXXXXXXXX",
                "inputmode": "numeric",
                "autocomplete": "tel-national",
                "pattern": "9\\d{9}",
                "maxlength": "10",
            }),
            "client_email": forms.EmailInput(attrs={"placeholder": "Owner email"}),
            "area": forms.Select(),
            "classification": forms.Select(),
            "area_value": forms.NumberInput(attrs={"step": "0.0001", "placeholder": "e.g. 250.0000"}),
            "area_unit": forms.Select(),
            "case_type": forms.Select(),
            "property_title_type": forms.Select(),
        }

    def __init__(self, *args, user: CustomUser | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if user and getattr(user, "role", None) == "lgu_admin":
            mun = (getattr(user, "lgu_municipality", "") or "").strip()
            if mun:
                self.fields["area"].choices = [(mun, mun)]
                self.initial.setdefault("area", mun)
                self.fields["area"].disabled = True

    def clean(self):
        cleaned = super().clean() or {}
        # Enforce required fields for the new request form.
        if not (cleaned.get("client_first_name") or "").strip():
            self.add_error("client_first_name", "First name is required.")
        if not (cleaned.get("client_last_name") or "").strip():
            self.add_error("client_last_name", "Last name is required.")
        if not (cleaned.get("case_type") or "").strip():
            self.add_error("case_type", "Type of transaction is required.")
        
        classification = cleaned.get("classification")
        if not classification:
            self.add_error("classification", "Classification is required.")
            
        area_value = cleaned.get("area_value")
        area_unit = cleaned.get("area_unit")
        if area_value is None and not area_unit:
            self.add_error("area_value", "Property area is required.")
        elif area_value is not None and not area_unit:
            self.add_error("area_unit", "Area unit is required.")
        elif area_value is None and area_unit:
            self.add_error("area_value", "Area value is required.")

        raw_num = (cleaned.get("client_number") or "").strip()
        if raw_num:
            # Step 1: Extract all digits
            digits = "".join([c for c in raw_num if c.isdigit()])
            
            # Step 2: Handle common PH prefixes to get the base 10 digits
            if len(digits) == 12 and digits.startswith("63"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            
            # Step 3: Validate base 10 digits
            if len(digits) != 10 or not digits.startswith("9"):
                self.add_error("client_number", "Enter a valid 10-digit number starting with 9.")
            else:
                # STORE only the 10 digits
                cleaned["client_number"] = digits

        case_type = (cleaned.get("case_type") or "").strip()
        title_type = (cleaned.get("property_title_type") or "").strip()
        if case_type in {"land_first_time", "transfer_ownership_tax_decl"}:
            if title_type not in {"titled", "untitled"}:
                self.add_error("property_title_type", "Please select whether the property is titled or untitled.")
        else:
            cleaned["property_title_type"] = ""
        return cleaned


class ChecklistItemForm(forms.Form):
    doc_type = forms.ChoiceField(required=False, choices=[("", "— Select —")])
    custom_doc_type = forms.CharField(max_length=120, required=False)
    file = forms.FileField(required=False)
    is_deleted = forms.BooleanField(required=False, initial=False, widget=forms.HiddenInput())

    def __init__(self, *args, doc_type_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "— Select —")]
        for c in (doc_type_choices or []):
            label = str(c).strip()
            if not label:
                continue
            choices.append((label, label))
        choices.append(("__custom__", "Other (type manually)"))
        self.fields["doc_type"].choices = choices
        self.fields["custom_doc_type"].widget.attrs.setdefault("placeholder", "Type document name")
        self.fields["file"].widget.attrs.update({
            "accept": ".pdf,.png,.jpg,.jpeg,.doc,.docx"
        })

    def clean(self):
        cleaned = super().clean() or {}
        selected = (cleaned.get("doc_type") or "").strip()
        custom = (cleaned.get("custom_doc_type") or "").strip()

        doc_type = ""
        if selected == "__custom__":
            doc_type = custom
        else:
            doc_type = selected

        if not doc_type and (cleaned.get("file") or selected == "__custom__" or custom):
            raise forms.ValidationError("Document type is required for this row.")
        cleaned["doc_type"] = doc_type
        return cleaned

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if not f:
            return f

        max_mb = int(getattr(settings, "MAX_UPLOAD_SIZE_MB", 25) or 25)
        max_bytes = max_mb * 1024 * 1024
        if getattr(f, "size", 0) and f.size > max_bytes:
            raise ValidationError(f"File too large. Maximum allowed is {max_mb}MB.")

        allowed = getattr(
            settings,
            "ALLOWED_UPLOAD_EXTENSIONS",
            {".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx"},
        )
        name = getattr(f, "name", "") or ""
        ext = os.path.splitext(name)[1].lower()
        if ext and ext not in set(allowed):
            raise ValidationError("Unsupported file type.")
        return f


class CaseRemarkForm(forms.Form):
    text = forms.CharField(
        label="Remark / Comment",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Add an internal note..."}),
    )

    def clean_text(self):
        cleaned = self.cleaned_data or {}
        text = (cleaned.get("text") or "").strip()
        if not text:
            raise ValidationError("Remark cannot be empty.")
        return text


def build_checklist_formset(*, initial=None, extra: int = 5):
    FormSet = forms.formset_factory(ChecklistItemForm, extra=extra)
    return FormSet(initial=initial or [])


class StaffAccountCreateForm(forms.ModelForm):
    account_type = forms.ChoiceField(
        choices=[("capitol", "Capitol Admin"), ("lgu", "LGU Admin")],
        initial="capitol",
        widget=forms.Select(),
    )
    capitol_role = forms.ChoiceField(
        required=False,
        choices=[
            ("capitol_receiving", "Receiver"),
            ("capitol_examiner", "Examiner"),
            ("capitol_approver", "Approver"),
            ("capitol_taxmapper", "Tax Mapper"),
            ("capitol_numberer", "Numberer"),
            ("capitol_releaser", "Releaser"),
        ],
        widget=forms.Select(),
    )

    lgu_municipality = forms.ChoiceField(
        required=False,
        choices=CustomUser.LGU_MUNICIPALITY_CHOICES,
        widget=forms.Select(),
    )

    class Meta:
        model = CustomUser
        fields: ClassVar[list[str]] = ["email", "first_name", "last_name"]

    def clean_email(self):
        cleaned = self.cleaned_data or {}
        email = (cleaned.get("email") or "").strip().lower()
        if not email:
            raise ValidationError("Email is required.")
        if CustomUser.objects.filter(email=email).exists():
            raise ValidationError("This email is already in use.")
        return email

    def clean(self):
        cleaned = super().clean() or {}
        account_type = cleaned.get("account_type")
        capitol_role = cleaned.get("capitol_role")
        lgu_municipality = (cleaned.get("lgu_municipality") or "").strip()

        if account_type == "lgu":
            if not lgu_municipality:
                raise ValidationError("Please select an LGU municipality assignment.")
        elif account_type == "capitol":
            if not capitol_role:
                raise ValidationError("Please select a position.")
            # Clear LGU for capitol accounts
            cleaned["lgu_municipality"] = ""
        else:
            raise ValidationError("Invalid account type.")

        return cleaned

    def save(self, commit=True):
        user: CustomUser = super().save(commit=False)
        cleaned = self.cleaned_data or {}

        account_type = cleaned.get("account_type")
        user.lgu_municipality = str(cleaned.get("lgu_municipality") or "")
        if account_type == "capitol":
            user.role = str(cleaned.get("capitol_role") or "")
        else:
            user.role = "lgu_admin"

        # Keep legacy full_name populated for existing templates.
        first_name = (cleaned.get("first_name") or "").strip()
        last_name = (cleaned.get("last_name") or "").strip()
        full_name = f"{first_name} {last_name}".strip()
        if full_name:
            user.full_name = full_name

        if commit:
            user.save()
        return user


class ProfileUpdateForm(forms.ModelForm):
    email_verify = forms.EmailField(
        label="Confirm your email",
        help_text="Enter your email to confirm changes.",
        required=True,
        widget=forms.EmailInput(attrs={"placeholder": "your@email.com"}),
    )

    class Meta:
        model = CustomUser
        fields: ClassVar[list[str]] = ["photo", "username", "position"]

    def __init__(self, *args, user: CustomUser, **kwargs):
        super().__init__(*args, **kwargs)
        self._user = user
        for name, field in self.fields.items():
            attrs = dict(getattr(field.widget, "attrs", {}) or {})
            if name == "photo":
                attrs.setdefault("accept", "image/*")
            attrs.setdefault("class", "form-control")
            field.widget.attrs = attrs

    def clean_email_verify(self):
        cleaned = self.cleaned_data or {}
        email_verify = (cleaned.get("email_verify") or "").strip().lower()
        if email_verify != (self._user.email or "").strip().lower():
            raise ValidationError("Email verification does not match your account email.")
        return email_verify

    def clean_username(self):
        cleaned = self.cleaned_data or {}
        username = (cleaned.get("username") or "").strip()
        if not username:
            raise ValidationError("Username (Staff ID) is required.")
        qs = CustomUser.objects.filter(username__iexact=username).exclude(id=self._user.id)
        if qs.exists():
            raise ValidationError("This Staff ID is already in use.")
        return username

    def clean_photo(self):
        f = self.cleaned_data.get("photo")
        if not f:
            return f
        max_size = 2 * 1024 * 1024
        if getattr(f, "size", 0) > max_size:
            raise ValidationError("Photo must be 2MB or smaller.")
        return f


class SettingsProfileForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields: ClassVar[list[str]] = ["photo", "first_name", "last_name", "position"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            attrs = dict(getattr(field.widget, "attrs", {}) or {})
            if name == "photo":
                attrs.setdefault("accept", "image/*")
            attrs.setdefault("class", "form-control")
            field.widget.attrs = attrs

    def clean_photo(self):
        f = self.cleaned_data.get("photo")
        if not f:
            return f
        max_size = 2 * 1024 * 1024
        if getattr(f, "size", 0) > max_size:
            raise ValidationError("Photo must be 2MB or smaller.")
        return f


class SettingsNotificationsForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields: ClassVar[list[str]] = [
            "notify_new_account_activations",
            "notify_weekly_activity_report",
            "notify_critical_system_alerts",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            attrs = dict(getattr(field.widget, "attrs", {}) or {})
            attrs.setdefault("class", "form-control")
            field.widget.attrs = attrs


class SettingsPreferencesForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields: ClassVar[list[str]] = [
            "timezone_preference",
            "date_format_preference",
            "theme_preference",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["timezone_preference"].widget = forms.Select(choices=[
            ("Asia/Manila", "(GMT+08:00) Asia/Manila (Philippines)"),
            ("UTC", "UTC / GMT"),
        ])
        self.fields["date_format_preference"].widget = forms.Select(choices=[
            ("YYYY-MM-DD", "YYYY-MM-DD (2026-12-31)"),
            ("MM/DD/YYYY", "MM/DD/YYYY (12/31/2026)"),
            ("DD/MM/YYYY", "DD/MM/YYYY (31/12/2026)"),
        ])
        for field in self.fields.values():
            attrs = dict(getattr(field.widget, "attrs", {}) or {})
            attrs.setdefault("class", "form-control")
            field.widget.attrs = attrs


class StaffSearchForm(forms.Form):
    q = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Search users by name or ID"}),
    )
    role = forms.ChoiceField(
        required=False,
        choices=[("", "All Roles"), *CustomUser.ROLE_CHOICES],
        widget=forms.Select(),
    )


class AccountActivationForm(forms.Form):
    temp_password = forms.CharField(
        label="Temporary Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    new_password1 = forms.CharField(
        label="New Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
    )
    new_password2 = forms.CharField(
        label="Confirm New Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
    )

    def __init__(self, user: CustomUser, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_temp_password(self):
        cleaned = self.cleaned_data or {}
        temp_password = cleaned.get("temp_password") or ""
        if self.user.account_status != "pending":
            raise ValidationError("This account is not pending activation.")
        if self.user.temp_password_created_at:
            if timezone.now() - self.user.temp_password_created_at > timedelta(days=7):
                raise ValidationError("Temporary password expired. Contact the Super Admin for a resend.")
        if not self.user.check_password(temp_password):
            raise ValidationError("Temporary password is incorrect.")
        return temp_password

    def clean(self):
        cleaned = super().clean() or {}
        pw1 = cleaned.get("new_password1")
        pw2 = cleaned.get("new_password2")
        if pw1 and pw2 and pw1 != pw2:
            raise ValidationError("New passwords do not match.")
        if pw1:
            validate_password(pw1, self.user)
        return cleaned

    def save(self):
        cleaned = self.cleaned_data or {}
        pw1 = cleaned.get("new_password1")
        if not pw1:
            raise ValidationError("New password is required.")
        self.user.set_password(pw1)
        self.user.account_status = "active"
        self.user.is_active = True
        self.user.must_change_password = False
        self.user.activated_at = timezone.now()
        self.user.activation_nonce = ""
        self.user.save(update_fields=["password", "account_status", "is_active", "must_change_password", "activated_at", "activation_nonce"])
        return self.user


class StaffAccountUpdateForm(forms.ModelForm):
    lgu_municipality = forms.ChoiceField(
        required=False,
        choices=CustomUser.LGU_MUNICIPALITY_CHOICES,
        widget=forms.Select(),
        help_text="Assigned LGU municipality (used for dashboard visibility).",
    )

    class Meta:
        model = CustomUser
        fields: ClassVar[list[str]] = ["full_name", "designation", "position"]

    # Inside your CaseDetailsForm in forms.py
    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        
        if user:
            if user.role == 'lgu_admin':
                # LGU Admins have their origin locked to their own municipality
                if user.lgu_municipality:
                    self.fields['area'].initial = user.lgu_municipality
                    self.fields['area'].widget = forms.HiddenInput()
            
            elif user.role == 'capitol_receiving':
                # Receivers MUST choose an origin from the dropdown
                self.fields['area'].required = True
                self.fields['area'].label = "LGU Origin"
                self.fields['area'].help_text = "Select the municipality where this property is located."

    def clean_full_name(self):
        cleaned = self.cleaned_data or {}
        return (cleaned.get("full_name") or "").strip()

    def save(self, commit=True):
        user: CustomUser = super().save(commit=False)
        if "lgu_municipality" in self.cleaned_data:
            user.lgu_municipality = str(self.cleaned_data.get("lgu_municipality") or "")
        else:
            # If not in form, it might be a capitol user, so ensure it's empty
            if user.role != "lgu_admin":
                user.lgu_municipality = ""
        if commit:
            user.save()
        return user


class PublicCaseSearchForm(forms.Form):
    q = forms.CharField(
        label="Tracking Number",
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "e.g., PAS26A7S12B"}),
    )

    def clean_q(self):
        cleaned = self.cleaned_data or {}
        q = (cleaned.get("q") or "").strip().upper()
        if not q:
            raise ValidationError("Tracking number is required.")
        return q


class SupportFeedbackForm(forms.Form):
    name = forms.CharField(required=False, max_length=120)
    email = forms.EmailField(required=False)
    message = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Describe your concern..."}),
    )

    def clean_message(self):
        cleaned = self.cleaned_data or {}
        msg = (cleaned.get("message") or "").strip()
        if not msg:
            raise ValidationError("Message is required.")
        return msg


class ReportFilterForm(forms.Form):
    REPORT_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("status_breakdown", "Status Breakdown"),
        ("monthly_accomplishment", "Monthly Accomplishment"),
        ("processing_times", "Processing Times"),
    ]

    report_type = forms.ChoiceField(choices=REPORT_CHOICES, required=True)
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All Statuses"), *Case.STATUS_CHOICES],
    )
    sort = forms.ChoiceField(
        required=False,
        choices=[
            ("-created_at", "Newest"),
            ("created_at", "Oldest"),
            ("-updated_at", "Recently Updated"),
        ],
    )

    def clean(self):
        cleaned = super().clean() or {}
        d1 = cleaned.get("date_from")
        d2 = cleaned.get("date_to")
        if d1 and d2 and d1 > d2:
            raise ValidationError("Date From must be on or before Date To.")
        return cleaned
