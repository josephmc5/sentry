from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_syncdb, post_save, pre_delete
from pkg_resources import parse_version as Version

from sentry.constants import MEMBER_OWNER, MEMBER_USER
from sentry.db.models import update
from sentry.db.models.utils import slugify_instance
from sentry.models import (
    Project, User, Option, Team, ProjectKey, UserOption, TagKey, TagValue,
    GroupTag, GroupTagKey, Activity, TeamMember, Alert)
from sentry.signals import buffer_incr_complete, regression_signal
from sentry.utils.safe import safe_execute


def create_default_project(created_models, verbosity=2, **kwargs):
    if Project not in created_models:
        return
    if Project.objects.filter(id=settings.SENTRY_PROJECT).exists():
        return

    try:
        user = User.objects.filter(is_superuser=True)[0]
    except IndexError:
        user = None

    project = Project.objects.create(
        public=False,
        name='Sentry (Internal)',
        slug='sentry',
        owner=user,
        platform='django',
    )
    # HACK: manually update the ID after insert due to Postgres
    # sequence issues. Seriously, fuck everything about this.
    # TODO(dcramer): find a better solution
    if project.id != settings.SENTRY_PROJECT:
        project.key_set.all().delete()
        project.update(id=settings.SENTRY_PROJECT)
        create_team_and_keys_for_project(project, created=True)

    if verbosity > 0:
        print 'Created internal Sentry project (slug=%s, id=%s)' % (project.slug, project.id)


def set_sentry_version(latest=None, **kwargs):
    import sentry
    current = sentry.get_version()

    version = Option.objects.get_value(
        key='sentry:latest_version',
        default=''
    )

    for ver in (current, version):
        if Version(ver) >= Version(latest):
            return

    Option.objects.set_value(
        key='sentry:latest_version',
        value=(latest or current)
    )


def create_team_and_keys_for_project(instance, created, **kwargs):
    if not created or kwargs.get('raw'):
        return

    if not instance.owner:
        return

    if not instance.team:
        team = Team(owner=instance.owner, name=instance.name)
        slugify_instance(team, instance.slug)
        team.save()
        update(instance, team=team)

    if not ProjectKey.objects.filter(project=instance, user__isnull=True).exists():
        ProjectKey.objects.create(
            project=instance,
        )


def create_team_member_for_owner(instance, created, **kwargs):
    if not created:
        return

    if not instance.owner:
        return

    instance.member_set.get_or_create(
        user=instance.owner,
        type=MEMBER_OWNER,
    )


def remove_key_for_team_member(instance, **kwargs):
    for project in instance.team.project_set.all():
        ProjectKey.objects.filter(
            project=project,
            user=instance.user,
        ).delete()


# Set user language if set
def set_language_on_logon(request, user, **kwargs):
    language = UserOption.objects.get_value(
        user=user,
        project=None,
        key='language',
        default=None,
    )
    if language and hasattr(request, 'session'):
        request.session['django_language'] = language


@buffer_incr_complete.connect(sender=TagValue, weak=False)
def record_project_tag_count(filters, created, **kwargs):
    from sentry import app

    if not created:
        return

    app.buffer.incr(TagKey, {
        'values_seen': 1,
    }, {
        'project': filters['project'],
        'key': filters['key'],
    })


@buffer_incr_complete.connect(sender=GroupTag, weak=False)
def record_group_tag_count(filters, created, **kwargs):
    from sentry import app

    if not created:
        return

    app.buffer.incr(GroupTagKey, {
        'values_seen': 1,
    }, {
        'project': filters['project'],
        'group': filters['group'],
        'key': filters['key'],
    })


@regression_signal.connect(weak=False)
def create_regression_activity(instance, **kwargs):
    if instance.times_seen == 1:
        # this event is new
        return
    Activity.objects.create(
        project=instance.project,
        group=instance,
        type=Activity.SET_REGRESSION,
    )


def on_alert_creation(instance, **kwargs):
    from sentry.plugins import plugins

    for plugin in plugins.for_project(instance.project):
        safe_execute(plugin.on_alert, alert=instance)

def add_user_to_projects(request, user, **kwargs):
    for team in Team.objects.all():
        if not Project.objects.filter(id=settings.SENTRY_PROJECT,team=team).exists():
            TeamMember.objects.get_or_create(
                user=user,
                team=team)

# Signal registration
post_syncdb.connect(
    create_default_project,
    dispatch_uid="create_default_project",
    weak=False,
)
post_save.connect(
    create_team_and_keys_for_project,
    sender=Project,
    dispatch_uid="create_team_and_keys_for_project",
    weak=False,
)
post_save.connect(
    create_team_member_for_owner,
    sender=Team,
    dispatch_uid="create_team_member_for_owner",
    weak=False,
)
pre_delete.connect(
    remove_key_for_team_member,
    sender=TeamMember,
    dispatch_uid="remove_key_for_team_member",
    weak=False,
)
user_logged_in.connect(
    set_language_on_logon,
    dispatch_uid="set_language_on_logon",
    weak=False
)
user_logged_in.connect(
    add_user_to_projects,
    dispatch_uid="add_user_to_projects",
    weak=False
)
post_save.connect(
    on_alert_creation,
    sender=Alert,
    dispatch_uid="on_alert_creation",
    weak=False,
)
