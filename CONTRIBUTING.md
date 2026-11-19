Contributing to GhostAP

Contributions, bug reports, hardware compatibility reports, and feature suggestions are welcome.

Reporting Bugs

When opening an issue, please include:

* Hardware model.
* Operating system and architecture.
* GhostAP version or commit.
* Steps to reproduce the problem.
* Expected and actual behaviour.
* Relevant logs and diagnostic output, with sensitive information removed.

Security vulnerabilities should be reported privately as described in SECURITY.md.

Submitting Changes

1. Fork the repository.
2. Create a branch for your changes.
3. Make focused changes related to the issue or feature.
4. Run the automated test suite.
5. Update relevant documentation and tests.
6. Submit a pull request describing the changes and how they were tested.

For substantial architectural changes, please open an issue for discussion before starting implementation.

Development Principles

GhostAP prioritises reliability, simplicity, and safe network recovery.

Contributions should:

* Preserve the event-driven architecture and avoid unnecessary background polling.
* Maintain safe rollback and recovery behaviour.
* Avoid unnecessary dependencies.
* Preserve privilege separation and protect sensitive configuration.
* Maintain compatibility with supported hardware and operating systems.
* Include regression tests for bug fixes and tests for new functionality.

Hardware Testing

Hardware compatibility reports are particularly valuable. Refer to HARDWARE_TEST.md for hardware validation procedures.

Clearly distinguish between automated testing and testing performed on physical hardware.

License

By submitting a contribution, you agree that your contribution will be licensed under the GNU General Public License v3.0.