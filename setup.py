from setuptools import setup, find_packages

setup(
    name="gmail-hubspot-sync",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "google-api-python-client>=2.131.0",
        "google-auth>=2.29.0",
        "google-auth-httplib2>=0.2.0",
        "google-auth-oauthlib>=1.2.0",
        "requests>=2.32.3",
        "python-dotenv>=1.0.1",
    ],
    entry_points={
        "console_scripts": [
            "gmail-hubspot-sync=gmail_hubspot_sync.main:run",
        ]
    },
    python_requires=">=3.10",
)
