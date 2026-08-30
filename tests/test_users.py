import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

User = get_user_model()


@pytest.mark.django_db
def test_user_can_be_created():
    user = User.objects.create_user(email='owner@example.com', password='s3cret-pass')

    assert user.pk is not None
    assert user.email == 'owner@example.com'
    assert user.check_password('s3cret-pass')
    assert user.is_active
    assert not user.is_staff


@pytest.mark.django_db
def test_duplicate_email_is_rejected():
    User.objects.create_user(email='owner@example.com', password='s3cret-pass')

    with pytest.raises(IntegrityError):
        User.objects.create_user(email='owner@example.com', password='another-pass')
