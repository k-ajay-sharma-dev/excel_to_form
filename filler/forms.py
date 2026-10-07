from django import forms

from .models import AppSettings


class SettingsForm(forms.ModelForm):
    class Meta:
        model = AppSettings
        fields = ["form_url", "allow_submit", "headless", "slow_mo"]


class UploadForm(forms.Form):
    file = forms.FileField(label="Excel or CSV file")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".xlsx", ".xls", ".csv")):
            raise forms.ValidationError("Please upload an .xlsx, .xls or .csv file.")
        return f
