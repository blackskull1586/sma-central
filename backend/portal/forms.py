from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.db.models import Q
from .models import User, Environment, Channel, Unit
from .access import valid_unit_scope

def available_username(value):
    value = value.strip().lower()
    User._meta.get_field('username').run_validators(value)
    if User.objects.filter(username__iexact=value).exists():
        raise ValidationError('Este nome de usuário já está em uso.')
    return value

class LoginForm(AuthenticationForm):
    def clean_username(self):
        return self.cleaned_data['username'].strip().lower()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not valid_unit_scope(user):
            raise ValidationError('Acesso indisponível. Procure o administrador.', code='inactive')
        owner_portal = getattr(self.request, 'is_owner_portal', False)
        if (settings.SMA_OWNER_HOST and user.is_owner and not owner_portal
                and not getattr(self.request, 'is_shared_portal', False)):
            raise ValidationError('Entre pelo endereço %(host)s.', code='invalid_login',
                                  params={'host': settings.SMA_OWNER_HOST})
        if owner_portal and not user.is_owner:
            raise ValidationError('Use uma conta de proprietário.', code='invalid_login')
        domain_environment = getattr(self.request, 'domain_environment', None)
        if domain_environment and not user.is_owner and user.environment_id != domain_environment.pk:
            raise ValidationError('Use uma conta deste ambiente.', code='invalid_login')
        if not user.is_owner and not user.environment.active:
            raise ValidationError('Acesso indisponível. Procure o administrador.', code='inactive')

class EnvironmentCreateForm(forms.Form):
    name = forms.CharField(label='Nome do servidor SMA', max_length=80)
    slug = forms.SlugField(label='Identificador do servidor', max_length=50, help_text='Exemplo: sma6. Use letras, números e hífens, sem espaços.')
    username = forms.CharField(label='Usuário do administrador', max_length=150)
    password1 = forms.CharField(label='Senha inicial do administrador', widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}), help_text='Use pelo menos 12 caracteres. A troca será exigida no primeiro acesso.')
    password2 = forms.CharField(label='Confirme a senha', widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

    def clean_username(self):
        return available_username(self.cleaned_data['username'])

    def clean_slug(self):
        slug = self.cleaned_data['slug'].lower()
        if Environment.objects.filter(slug=slug).exists():
            raise ValidationError('Este identificador já está em uso.')
        return slug

    def clean(self):
        data = super().clean()
        if data.get('password1') != data.get('password2'):
            self.add_error('password2', 'As senhas não coincidem.')
        if data.get('password1'):
            try:
                password_validation.validate_password(data['password1'], User(username=data.get('username', '')))
            except ValidationError as error:
                self.add_error('password1', error)
        return data

def add_management_context(form, environment):
    """Catch an SMA switch in another tab without making posted data authoritative."""
    expected = str(environment.pk) if environment is not None else ''

    def same_environment(value):
        values = form.data.getlist('management_environment') if hasattr(form.data, 'getlist') else [value]
        if value and (value != expected or len(values) != 1):
            raise ValidationError('O servidor selecionado mudou em outra aba. Volte aos servidores, '
                                  'escolha o SMA desejado e abra este cadastro novamente.')

    form.fields['management_environment'] = forms.CharField(
        widget=forms.HiddenInput, required=False, initial=expected, validators=[same_environment])


class AccountCreateForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ['username', 'first_name', 'role']
        labels = {'first_name': 'Nome de exibição', 'role': 'Tipo de acesso'}

    def __init__(self, *args, actor, environment=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(User.Role.VIEWER, 'Visualizador')]
        if actor.is_owner:
            choices.append((User.Role.ADMIN, 'Administrador'))
            self.fields['role'].help_text = ('Administrador: gerencia canais e usuários visualizadores do seu ambiente. '
                'Visualizador: assiste aos canais autorizados, sem administrar nem consultar chaves.')
            self.fields['username'].label = 'Usuário de acesso'
            self.fields['username'].help_text = 'Nome usado no login, sem espaços. Exemplo: operador_norte.'
            self.fields['first_name'].help_text = 'Nome da pessoa ou equipe, exibido no sistema (opcional).'
            self.fields['password1'].label = 'Senha inicial'
            self.fields['password2'].label = 'Confirme a senha inicial'
            self.fields['unit'] = forms.ModelChoiceField(
                label='Ambiente da conta', queryset=Unit.objects.filter(environment=environment),
                required=False, empty_label='Área inicial (sem ambiente vinculado)',
                help_text='Define o que o administrador pode gerenciar. Quem pode assistir é configurado em cada canal.')
            add_management_context(self, environment)
            self.order_fields(['username', 'first_name', 'role', 'unit', 'password1', 'password2'])
        # Assign the complete choices once so the rendered select and validation agree.
        self.fields['role'].choices = choices
        self.fields['role'].initial = User.Role.VIEWER

    def clean_username(self):
        return available_username(self.cleaned_data['username'])


class AccountUnitForm(forms.Form):
    unit = forms.ModelChoiceField(label='Ambiente da conta', queryset=Unit.objects.none(),
        required=False, empty_label='Área inicial (sem ambiente vinculado)',
        help_text='Define o ambiente da conta e o que ela pode administrar. As permissões de assistir aos canais serão mantidas.')
    original_unit = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, environment, target, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['unit'].queryset = Unit.objects.filter(environment=environment)
        self.initial.update(unit=target.unit_id, original_unit=str(target.unit_id) if target.unit_id else 'none')


class UnitCreateForm(forms.Form):
    environment = forms.ModelChoiceField(label='Servidor SMA', queryset=Environment.objects.none(),
        empty_label='Selecione o servidor')
    name = forms.CharField(label='Nome do ambiente', max_length=80,
        help_text='Nome da unidade ou equipe. Exemplo: Equipe Norte.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['environment'].queryset = Environment.objects.filter(active=True)

    def clean(self):
        data = super().clean()
        if data.get('environment') and data.get('name') and Unit.objects.filter(
                environment=data['environment'], name__iexact=data['name']).exists():
            self.add_error('name', 'Já existe um ambiente com esse nome neste servidor.')
        return data


class UnitEditForm(forms.Form):
    name = forms.CharField(label='Nome do ambiente', max_length=80)

    def __init__(self, *args, unit, **kwargs):
        super().__init__(*args, **kwargs)
        self.unit = unit
        self.initial['name'] = unit.name

    def clean_name(self):
        name = self.cleaned_data['name']
        if Unit.objects.filter(environment=self.unit.environment, name__iexact=name).exclude(pk=self.unit.pk).exists():
            raise ValidationError('Já existe um ambiente com esse nome neste servidor.')
        return name


class ChannelViewerChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, user):
        unit_name = user.unit.name if user.unit_id else 'Área inicial (sem ambiente vinculado)'
        status = ' · Desativado' if not user.is_active else ''
        return f'{user.username} · {unit_name} · {user.get_role_display()}{status}'


class ChannelViewersForm(forms.Form):
    allowed_viewers = ChannelViewerChoiceField(
        label='Quem pode assistir', queryset=User.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text='A seleção permite apenas assistir. Não concede acesso à administração nem à chave de transmissão.')

    def __init__(self, *args, channel, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['allowed_viewers'].queryset = User.objects.filter(
            environment_id=channel.environment_id, role__in=[User.Role.ADMIN, User.Role.VIEWER],
        ).filter(Q(unit__isnull=True) | Q(unit__environment_id=channel.environment_id)
        ).select_related('unit').order_by('unit__name', 'username')
        self.initial['allowed_viewers'] = list(channel.allowed_viewers.values_list('pk', flat=True))


class ChannelForm(forms.ModelForm):
    class Meta:
        model = Channel
        fields = ['name']

    def __init__(self, *args, environment, unit=None, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.environment = environment
        self.unit = unit
        if actor is not None and actor.is_owner:
            self.fields['name'].label = 'Nome do canal'
            self.fields['name'].help_text = 'Todos os vídeos enviados por esta chave exibirão exatamente este nome.'
            if self.instance._state.adding:
                self.fields['unit'] = forms.ModelChoiceField(
                    label='Ambiente responsável', queryset=Unit.objects.filter(environment=environment),
                    required=False, empty_label='Área inicial (sem ambiente vinculado)',
                    help_text='Os administradores deste ambiente poderão gerenciar o canal. '
                              'Depois de criar, defina separadamente quem pode assistir.')
                add_management_context(self, environment)

    def clean_name(self):
        return self.cleaned_data['name'].strip()

    def clean(self):
        data = super().clean()
        name = data.get('name')
        if not name or ('unit' in self.fields and 'unit' not in data):
            return data
        unit = data.get('unit') if 'unit' in self.fields else self.unit
        matches = Channel.objects.filter(environment=self.environment, unit=unit, name__iexact=name)
        if self.instance.pk:
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            self.add_error('name', 'Já existe um canal com esse nome neste ambiente.')
        return data
