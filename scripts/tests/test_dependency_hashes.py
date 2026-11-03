import pytest
from scripts.lock_dependency_hashes import pins,render


def payload():
    return {'info':{'name':'sample','version':'1.0'},'vulnerabilities':[],
        'urls':[{'digests':{'sha256':'a'*64},'yanked':False},{'digests':{'sha256':'b'*64},'yanked':True}]}


def test_published_file_hashes_preserve_pins_and_reject_yanked_files():
    source,report=render('sample==1.0',lambda _:payload())
    assert pins(source)==[('sample','1.0')] and report['file_hashes']==1
    assert 'a'*64 in source and 'b'*64 not in source


def test_hashing_cannot_silently_bind_another_package_or_vulnerable_version():
    data=payload();data['info']['name']='other'
    with pytest.raises(ValueError):render('sample==1.0',lambda _:data)
    data=payload();data['vulnerabilities']=[{'id':'synthetic-advisory'}]
    with pytest.raises(ValueError):render('sample==1.0',lambda _:data)


def test_unpinned_urls_and_empty_locks_are_rejected():
    for text in ('sample>=1.0','sample @ https://example.invalid/pkg.whl',''):
        with pytest.raises(ValueError):pins(text)
