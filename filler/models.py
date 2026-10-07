from django.conf import settings
from django.db import models

DEFAULT_FORM_URL = "https://ee-eu.kobotoolbox.org/x/NYkgOz8F"
PROBLEM = ("FAILED", "SKIPPED")


class AppSettings(models.Model):
    """Single row (pk=1) edited on the Settings page."""
    form_url = models.URLField("Kobo / Enketo form link", default=DEFAULT_FORM_URL)
    allow_submit = models.BooleanField(
        "Allow real submission", default=False,
        help_text="Off = Submit only does a full check run; nothing is ever sent to Kobo.",
    )
    headless = models.BooleanField("Run browser hidden", default=True)
    slow_mo = models.PositiveIntegerField("Slow-motion delay (ms)", default=0)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def env_locked(self):
        return not settings.SUBMIT_ENV_UNLOCKED

    @property
    def submit_enabled(self):
        """Real submission needs BOTH the Settings switch and the server-side lock to be open."""
        return self.allow_submit and settings.SUBMIT_ENV_UNLOCKED


class Job(models.Model):
    STATUS = [("queued", "Queued"), ("processing", "Processing"), ("done", "Done"), ("error", "Error")]
    file = models.FileField(upload_to="uploads/")
    name = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS, default="queued")
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-created"]

    @property
    def busy(self):
        return self.status in ("queued", "processing") or self.rows.filter(status__in=Row.BUSY).exists()


class Row(models.Model):
    STATUS = [
        ("pending", "Waiting"),
        ("filling", "Checking…"),
        ("ready", "Ready to submit"),
        ("review", "Needs review"),
        ("submitting", "Submitting…"),
        ("submitted", "Submitted"),
        ("dry_run", "Checked (submission off)"),
        ("submit_failed", "Submit failed"),
        ("error", "Error"),
    ]
    BUSY = ("pending", "filling", "submitting")

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="rows")
    row_no = models.PositiveIntegerField()
    title = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    message = models.TextField(blank=True)
    screenshot = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["row_no"]

    @property
    def busy(self):
        return self.status in self.BUSY

    def flagged(self):
        return [f for f in self.fields.all() if f.needs_review]


class Field(models.Model):
    row = models.ForeignKey(Row, on_delete=models.CASCADE, related_name="fields")
    order = models.PositiveIntegerField()
    header = models.TextField()        # Excel header
    question = models.TextField()      # label looked up on the form
    instance = models.PositiveIntegerField(default=0)  # copy number inside a repeat group
    value = models.TextField(blank=True)               # what will be filled (user can edit)
    excel_value = models.TextField(blank=True)         # what the Excel said
    suggestion = models.TextField(blank=True)          # value from the other candidate column
    flags = models.JSONField(default=list)             # Excel 0/1 option columns
    note = models.TextField(blank=True)                # problem found while reading the Excel
    reviewed = models.BooleanField(default=False)
    status = models.TextField(blank=True)              # result of the last browser run
    kind = models.CharField(max_length=20, blank=True)  # radio / checkbox / text
    options = models.JSONField(default=list)           # [[value, label], ...] seen on the form

    class Meta:
        ordering = ["order"]

    @property
    def problem(self):
        if self.status.startswith(PROBLEM):
            return self.status
        if self.note and not self.reviewed:
            return self.note
        return ""

    @property
    def needs_review(self):
        return bool(self.problem)
