from setuptools import setup, find_packages

setup(
    name="mini-claude-code",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "anthropic>=0.42.0",
        "openai>=1.0.0",
        "rich>=13.0.0",
        "prompt_toolkit>=3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "mcc=mcc.cli:main",
        ],
    },
    python_requires=">=3.9",
)
