# flickrd

플리커 본인 계정의 사진 다운로드


## 사용방법

사용자와 앨범 정보 확인

    python3 flickrd.py -k <api_key> -s <secret_key> status

모든 사진 다운로드

    python3 flickrd.py -k <api_key> -s <secret_key> all

특정 앨범 다운로드(album-id는 `status` 명령어로 확인 가능)

    python3 flickrd.py -k <api_key> -s <secret_key> album <album-id>


특정 기간에 촬영한 사진 다운로드

    python3 flickrd.py -k <api_key> -s <secret_key> taken 20160101 20160531

특정 기간에 업로드한 사진 다운로드

    python3 flickrd.py -k <api_key> -s <secret_key> posted 20160601 20160607

플리커 사진 sync 유지 (기본은 5분 단위 새로운 사진 체크)

    python3 flickrd.py -k <api_key> -s <secret_key> sync
    
플리커 인증 정보 및 캐시 삭제

    python3 flickrd.py delete-cache


## 기타 옵션

* -d (--download_dir) 다운로드 폴더 지정
* -n (--naming_rule) exif 정보에 따른 파일명 생성 규칙 지정
* -r (--subdir_rule) exif 촬영일시에 기반하여 일자별 하위 폴더 생성 규칙 지정 
* -y 질문없이 바로 다운로드 실행
* -h 도움말 출력


## 플리커 인증

* 본인이 사용 할 [API KEY](https://www.flickr.com/services/api/keys/) 생성 필요 
* 비공개 사진에 접근하기 위해 Flickr OAuth 인증 필요(최초 실행 시 1회만 수행)


## 환경 파일

최초 실행 시 동일 폴더에 `flickrd.ini`로 자동 생성.

```
[flickrd]
api_key = <your api key>
secret_key = <your secret key>
download_dir = flickr_photos
naming_rule = YYYY-MM-DD_hhmmss(camera)
subdir_rule =
```

## 필요 사항

* python3
* sqlalchemy
* flickrapi
* urllib3 (1.16 이상) 

필요 모듈 한방에 설치: `pip install --upgrade sqlalchemy flickrapi urllib3`


## 제약 사항

* 파이썬3만 지원
* 비디오는 다운로드 않음
* flickr api 회수 제한으로 다운로드 중 오류 발생할 수 있음 (30분 정도 후에 재시도)
 