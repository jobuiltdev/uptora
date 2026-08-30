from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


def normalize_email_in(data, field='email'):
    """Return `data` with `field` normalized, so field validators see the final value."""
    if isinstance(data, dict) and isinstance(data.get(field), str):
        data = data.copy()
        data[field] = User.objects.normalize_email(data[field].strip())
    return data


class UserSerializer(serializers.ModelSerializer):
    """Public representation of a user. Never includes the password hash."""

    class Meta:
        model = User
        fields = ('id', 'email', 'first_name', 'last_name', 'date_joined')
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})

    class Meta:
        model = User
        fields = ('id', 'email', 'password')

    def to_internal_value(self, data):
        # Normalize before super(), otherwise the uniqueness check runs against
        # the raw address and 'A@Example.com' would slip past an existing user.
        return super().to_internal_value(normalize_email_in(data))

    def validate(self, attrs):
        # Validated at object level so the similarity validator can compare the
        # password against the email being registered.
        try:
            password_validation.validate_password(
                attrs['password'], User(email=attrs.get('email', ''))
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)}) from exc
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login serializer that normalizes the email the same way registration does."""

    def to_internal_value(self, data):
        return super().to_internal_value(normalize_email_in(data, self.username_field))


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()
