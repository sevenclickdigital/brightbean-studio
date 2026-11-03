"""Forms for the Unified Social Inbox."""

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import InboxMessage, InboxSLAConfig, SavedReply


class ReplyForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": _("Write a reply..."),
                "class": "form-input w-full",
            }
        ),
    )


class InternalNoteForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Add an internal note..."),
                "class": "form-input w-full",
            }
        ),
    )


class AssignForm(forms.Form):
    assigned_to = forms.UUIDField(required=False)


class StatusForm(forms.Form):
    status = forms.ChoiceField(choices=InboxMessage.Status.choices)


class SentimentForm(forms.Form):
    sentiment = forms.ChoiceField(choices=InboxMessage.Sentiment.choices)


class BulkActionForm(forms.Form):
    message_ids = forms.CharField()
    action = forms.ChoiceField(
        choices=[
            ("mark_read", _("Mark as Read")),
            ("resolve", _("Resolve")),
            ("archive", _("Archive")),
            ("assign", _("Assign")),
        ]
    )
    value = forms.CharField(required=False)

    def clean_message_ids(self):
        raw = self.cleaned_data["message_ids"]
        return [mid.strip() for mid in raw.split(",") if mid.strip()]


class SavedReplyForm(forms.ModelForm):
    class Meta:
        model = SavedReply
        fields = ["title", "body"]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "form-input w-full",
                    "placeholder": _("Reply title"),
                }
            ),
            "body": forms.Textarea(
                attrs={
                    "class": "form-input w-full",
                    "rows": 4,
                    "placeholder": _("Reply body. Use {sender_name}, {account_name}, {post_url} for variables."),
                }
            ),
        }


class SLAConfigForm(forms.ModelForm):
    class Meta:
        model = InboxSLAConfig
        fields = ["target_response_minutes", "is_active", "auto_resolve_on_reply"]
        widgets = {
            "target_response_minutes": forms.NumberInput(
                attrs={
                    "class": "form-input w-full",
                    "min": 1,
                }
            ),
        }
