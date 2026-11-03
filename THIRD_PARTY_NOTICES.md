# Third-party components and data

The root MIT license covers the project contributors' own code. It does not
relicense dependencies, datasets, model weights, container images, or externally
owned code. Retain upstream copyright and license notices when distributing them.

Python dependencies are recorded in `backend/requirements.lock`; frontend
dependencies in `frontend/package-lock.json`. CI can generate dependency SBOMs.
These lockfiles identify the dependency set, not an assertion that every component
has the same license as this project.

MySQL, Redis, MinIO/S3-compatible servers, ClamAV, LM Studio and model weights are
separate components; consult their selected distributions and licenses. The
repository does not bundle those services or weights. An optional Docker template
does not replace distribution-license review.

COCO, PA-100K and other datasets mentioned in experiments are not included. Users
must obtain data from its original source, verify redistribution/training rights,
and preserve applicable per-image or dataset licenses. Test fixtures are synthetic.

The historical `usermanage` nested repository, private deployment records, chat
exports and career/resume drafts are excluded from the public-source export.
Do not assume that third-party code becomes MIT merely because it was copied into
a project. Preserve or add provenance before distributing such material.
