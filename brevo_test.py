import os
from dotenv import load_dotenv
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException


# Load variables from .env
load_dotenv()


def send_test_email():
    # Check required environment variables
    required_vars = [
        "BREVO_API_KEY",
        "BREVO_SENDER_EMAIL",
        "BREVO_SENDER_NAME",
        "BREVO_TEST_EMAIL",
    ]

    missing = [var for var in required_vars if not os.getenv(var)]

    if missing:
        print("❌ Missing environment variables:")
        for var in missing:
            print(f"   - {var}")
        return

    # Configure Brevo
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key["api-key"] = os.getenv("BREVO_API_KEY")

    api_client = sib_api_v3_sdk.ApiClient(configuration)
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(api_client)

    # Create email
    email = sib_api_v3_sdk.SendSmtpEmail(
        sender={
            "name": os.getenv("BREVO_SENDER_NAME"),
            "email": os.getenv("BREVO_SENDER_EMAIL"),
        },
        to=[
            {
                "email": os.getenv("BREVO_TEST_EMAIL"),
            }
        ],
        subject="Fidela Translations - Brevo API Test",
        html_content="""
        <html>
            <body>
                <h1>Hello from Fidela Translations! 🎉</h1>

                <p>
                    This is a test email sent through the
                    <strong>Brevo API</strong>.
                </p>

                <p>
                    If you received this email, the integration
                    between the Fidela Python backend and Brevo is working.
                </p>

                <hr>

                <p>
                    <strong>From:</strong>
                    support@fidelatranslations.ca
                </p>

                <p>
                    Fidela Translations
                </p>
            </body>
        </html>
        """,
    )

    # Send email
    try:
        response = api_instance.send_transac_email(email)

        print("✅ Email sent successfully!")
        print(f"Message ID: {response.message_id}")

    except ApiException as e:
        print("❌ Brevo API error:")
        print(e)


if __name__ == "__main__":
    send_test_email()