#!/usr/bin/env python3

import os
import re
import sys
import time
import codecs
import logging
import logging.handlers
import datetime
import argparse
import hashlib
import configparser
from urllib.request import urlretrieve

import flickrapi
import sqlalchemy
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

APPNAME = 'flickrd'
CONFIG_FILE = 'flickrd.ini'
LOGFILE = 'flickrd.log'
LOGLEVEL = logging.DEBUG
SYNC_SLEEP = 60 * 5     # 5분에 한번씩 sync
SKIP_CHECK_CNT = 100    # 100건 연속으로 sync한 사진이 나타나야 sync 완료로 판단
SQLITE_FILE = os.path.join(os.path.expandvars('$HOME'), '.flickr', 'flickrd.sqlite')

OPT = None
LOGGER = None
FLICKR = None
Base = declarative_base()
session = None


class FlickrPhoto(Base):
    __tablename__ = "flickr_photos"
    photo_id = sqlalchemy.Column(sqlalchemy.BIGINT, primary_key=True)
    width = sqlalchemy.Column(sqlalchemy.Integer, unique=False)
    height = sqlalchemy.Column(sqlalchemy.Integer, unique=False)
    model = sqlalchemy.Column(sqlalchemy.Text, unique=False)
    url = sqlalchemy.Column(sqlalchemy.Text, unique=False)
    date_taken = sqlalchemy.Column(sqlalchemy.DateTime, unique=False)
    date_posted = sqlalchemy.Column(sqlalchemy.DateTime, unique=False)
    date_lastupdate = sqlalchemy.Column(sqlalchemy.DateTime, unique=False)
    hash = sqlalchemy.Column(sqlalchemy.Text, unique=False)

    def __repr__(self):
        return "<Photo({}:{}x{} {:%Y/%m/%d %H:%M:%S} by {})>".format(
            self.photo_id, self.width, self.height, self.date_taken, self.model)


def delete_cache():
    try:
        auth_db = os.path.join(os.path.expanduser('~'), '.flickr', 'oauth-tokens.sqlite')
        LOGGER.debug('플리커 인증 정보 파일은 ({})입니다.'.format(auth_db))
        if os.path.exists(auth_db):
            os.unlink(auth_db)
            LOGGER.debug('플리커 인증 정보 파일을 정상 삭제했습니다.')
        else:
            LOGGER.debug('플리커 인증 정보 파일이 존재하지 않습니다.')

        photo_db = os.path.abspath(SQLITE_FILE)
        LOGGER.debug('사진 캐싱 정보 파일은 ({})입니다.'.format(photo_db))
        if os.path.exists(photo_db):
            os.unlink(photo_db)
            LOGGER.debug('사진 캐싱 정보 파일을 정상 삭제했습니다.')
        else:
            LOGGER.debug('사진 캐싱 정보 파일이 존재하지 않습니다.')
    except PermissionError as ex:
        LOGGER.error('정보 파일 삭제 중 권한 오류입니다: {}'.format(ex))
        sys.exit(1)

    LOGGER.info('플리커 인증 정보와 사진 캐싱 정보를 삭제했습니다.')
    return 0


def set_console_encoding():
    encoding = sys.stdout.encoding
    if encoding.replace('-', '').lower() not in ['utf8', 'cp949', 'euckr']:
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.detach())
        print('Warn! Current console encoding {} is not supported. Now, using UTF-8 encoding.'.format(encoding))


def _init():
    global OPT, LOGGER, session

    # 콘솔 출력 코덱 지정
    set_console_encoding()

    # 로깅 모듈 초기화
    LOGGER = logging.getLogger(APPNAME)
    datefmt = "%Y-%m-%d %H:%M:%S"
    stream_fmt = '%(message)s'
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(stream_fmt, datefmt))
    stream_handler.setLevel(logging.INFO)
    file_fmt = '[%(levelname)s] [%(filename)s:%(lineno)d] %(asctime)s.%(msecs).03d> %(message)s'
    file_handler = logging.handlers.RotatingFileHandler(LOGFILE, maxBytes=10*1024*1024, backupCount=0, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(file_fmt, datefmt))
    file_handler.setLevel(LOGLEVEL)
    LOGGER.addHandler(stream_handler)
    LOGGER.addHandler(file_handler)
    LOGGER.setLevel(logging.DEBUG)

    # 사용자 환경 설정 파일이 있으면 읽어들여서 기본값으로 세팅한다.
    defaults = {}
    if os.path.exists(CONFIG_FILE):
        config = configparser.ConfigParser()
        try:
            config.read(CONFIG_FILE)
            defaults = dict(config.items('flickrd'))
        except configparser.NoSectionError as ex:
            LOGGER.warn('환경설정 파일({})에서 [flickrd] 세션을 읽을 수가 없습니다: {}'.format(CONFIG_FILE, ex))
            LOGGER.warn('환경설정 파일은 무시하고 진행합니다.')
        except configparser.ParsingError as ex:
            LOGGER.error('환경설정 파일({}) 구문 오류입니다: {}'.format(CONFIG_FILE, ex))
            return 2

    # 실행 시 인자로 받을 항목을 정의한다. 인자로 지정한 옵션이 환경설정 파일보다 우선한다.
    parser = argparse.ArgumentParser(description='플리커에 올려진 앨범 또는 사진을 다운로드 합니다.',
                                     formatter_class=argparse.RawTextHelpFormatter,
                                     epilog='[사용예]\n' +
                                            'python flickrd.py status\n' +
                                            '  > 사용자 및 앨범 정보 조회 \n' +
                                            'python flickrd.py all -d D:\photos -r YYYY\MM \n' +
                                            '  > 모든 사진을 D:\Photos 아래에 년,월로 하위 폴더로 구분하여 다운로드 \n' +
                                            'python flickrd.py album 1122334455667788\n' +
                                            '  > 특정앨범 다운로드 (앨범번호는 status로 확인)\n' +
                                            'python flickrd.py taken 20150101\n' +
                                            '  > 특정일에 촬영한 사진 다운로드 \n' +
                                            'python flickrd.py posted 20150101 20150131\n' +
                                            '  > 특정 기간에 업로드한 사진 다운로드 \n' +
                                            '\n[기타]\n' +
                                            '모든 옵션은 환경파일 {}에 미리 정의할 수 있습니다.\n\n'.format(CONFIG_FILE))
    parser.add_argument('-k', '--api_key', action='store', metavar='<xxxxxxx>', help='[필수] flickr api key', default='')
    parser.add_argument('-s', '--secret_key', action='store', metavar='<sssssss>', help='[필수] flickr secret key',
                        default='')
    parser.add_argument('-d', '--download_dir', action='store', metavar='your_path', default='flickr_photos',
                        help='다운로드 폴더. 없으면 자동 생성하며 기본값은 flickr_photos')
    parser.add_argument('-n', '--naming_rule', action='store', metavar='<naming_rule>',
                        default='YYYY-MM-DD_hhmmss(camera)',
                        help='사진 저장 시 파일명 생성 규칙. 기본값은 YYYY-MM-DD_hhmmss(camera)\n' +
                             '다음 규칙에 의해 자동 변환 되며 대소문자에 주의 필요\n' +
                             ' * YYYY: exif의 촬영일시의 년. 예) 2016\n' +
                             ' * MM: exif의 촬영일시의 월. 예) 03\n' +
                             ' * DD: exif의 촬영일시의 일. 예) 02\n' +
                             ' * hh: exif의 촬영일시의 시. 예) 11\n' +
                             ' * mm: exif의 촬영일시의 분. 예) 22\n' +
                             ' * ss: exif의 촬영일시의 초. 예) 59\n' +
                             ' * camera: exif의 카메라 모델명. 예) iPhone 6\n' +
                             ' * photo_id: 플리커 포토 아이디. 예) 3348561253\n')
    parser.add_argument('-r', '--subdir_rule', action='store', metavar='<YYYYMM>', default='', nargs='?',
                        help='exif의 촬영일시에 기반하여 일자별 하위 폴더 자동 생성.\n' +
                             '중첩 디렉토리도 가능하여 대문자로만 입력필요. 예) YYYY{}YYYYMM'.format(os.sep, os.sep))
    parser.add_argument('command', action='store', nargs='*', metavar='Command',
                        help='status: 사용자와 및 앨범 정보를 출력한다\n' +
                             'all: 전체 사진을 다운로드 한다\n' +
                             'album <album-id>: 지정한 앨범번호의 모든 사진을 다운로드 한다\n' +
                             'taken <YYYYMMDD> <YYYYMMDD>: 지정한 기간에 찍은 사진을 다운로드 한다(종료일자는 생략 가능)\n' +
                             'posted <YYYYMMDD> <YYYYMMDD>: 지정한 기간에 올린 사진을 다운로드 한다(종료일자는 생략 가능)\n' +
                             'sync: 새로 올라온 사진을 다운로드 한다\n' +
                             'delete-cache: 플리커 인증 정보와 사진 캐싱 정보를 삭제한다\n')
    parser.add_argument('-y', action='store_true', dest='yes_anyway', help='질문 없이 다운로드를 시작한다')
    parser.set_defaults(**defaults)
    OPT = parser.parse_args()
    LOGGER.debug(OPT)

    # config file 업데이트: 한번 입력한 옵션은 두번 입력할 필요가 없도록 환경파일에 기록해둔다.
    LOGGER.debug('Config file is {}'.format(os.path.abspath(CONFIG_FILE)))
    with open(CONFIG_FILE, 'w') as configfile:
        configfile.write('[flickrd]\n' +
                         'api_key = {}\n'.format(OPT.api_key) +
                         'secret_key = {}\n'.format(OPT.secret_key) +
                         'download_dir = {}\n'.format(OPT.download_dir) +
                         'naming_rule = {}\n'.format(OPT.naming_rule) +
                         'subdir_rule = {}\n'.format(OPT.subdir_rule))

    if not OPT.api_key or not OPT.secret_key or len(OPT.command) == 0:
        # parser.print_help()
        LOGGER.error('api_key, secret_key, Command는 필수 입력입니다.')
        LOGGER.error('사용법을 확인하시려면 -h 옵션을 이용하세요.')
        return 1

    if not OPT.subdir_rule:  # -r 옵션에 값을 지정하지 않으면 None으로 되는데 이것을 ''으로 변경한다.
        OPT.subdir_rule = ''

    OPT.cmd = OPT.command[0].lower()  # 비교를 쉽게 하기 위해 소문자로 변경

    # 명령어 분석1: status와 all은 단독으로 쓰여야 한다.
    if OPT.cmd in ['status', 'all', 'sync', 'delete-cache']:
        if len(OPT.command) != 1:
            LOGGER.error('status, all, sync는 단독으로 사용해야 합니다. 오류 지시어: {}'.format(OPT.command[1:]))
            return 1
        if OPT.cmd == 'delete-cache':
            sys.exit(delete_cache())

    # 명령어 분석2:
    elif OPT.cmd == 'album':
        if len(OPT.command) == 1:
            LOGGER.error('앨범을 다운 받으려면 앨범 번호도 입력해야 합니다. 예) album 12345678901234567')
            return 1
        elif len(OPT.command) > 2:
            LOGGER.error('너무 많은 지시어를 사용했습니다. 오류 지시어: {}'.format(OPT.command[2:]))
            return 1
        if not re.match('^\d+$', OPT.command[1]):  # 플리커 앨범명은 17자리 숫자로 구성되어 있다.
            LOGGER.error('앨범 번호는 숫자로만 입력해야 합니다. 오류 지시어: {}'.format(OPT.command[1]))
            return 1
        OPT.album_id = OPT.command[1]

    # 명령어 분석3: 시작일자와 종료일자를 입력 검증. 종료일자는 생략 가능하다.
    elif OPT.cmd in ['taken', 'posted']:
        if len(OPT.command) == 1:
            LOGGER.error('다운 받을 기간도 입력해야 합니다. 예) {} 20160501 20160531 (종료일자는 생략 가능)'.format(OPT.cmd))
            return 1
        elif len(OPT.command) > 3:
            LOGGER.error('너무 많은 지시어를 사용했습니다. 오류 지시어: {}'.format(OPT.command[3:]))
            return 1

        # 시작일자 검증
        if not re.match('^\d{8}$', OPT.command[1]):
            LOGGER.error('시작일자는 YYYYMMDD 형식이어야 합니다. 오류 지시어: {}'.format(OPT.command[1]))
            return 1
        OPT.stdt = OPT.command[1]

        # 종료일자 검증
        if len(OPT.command) == 2:
            OPT.eddt = OPT.command[1]
        elif len(OPT.command) == 3:  # 종료일짜까지 입력한 경우
            if not re.match('^\d{8}$', OPT.command[2]):
                LOGGER.error('종료일자는 YYYYMMDD 형식이어야 합니다. 오류 지시어: {}'.format(OPT.command[2]))
                return 1
            OPT.eddt = OPT.command[2]

        time_adjust = datetime.datetime.now() - datetime.datetime.utcnow()  # 로컬시간과 UTC시간차
        OPT.stdt_stamp = datetime.datetime.strptime(OPT.stdt, '%Y%m%d')
        OPT.eddt_stamp = datetime.datetime.strptime(OPT.eddt, '%Y%m%d') + datetime.timedelta(days=1)
        OPT.stdt_stamp = int((OPT.stdt_stamp + time_adjust).timestamp())  # 로컬시간을 UTC 시간인 것처럼 조정해야 한다.
        OPT.eddt_stamp = int((OPT.eddt_stamp + time_adjust).timestamp())  # 로컬시간을 UTC 시간인 것처럼 조정해야 한다.

    else:
        LOGGER.error('[{}]는 유효한 명령어가 아닙니다. -h 옵션을 참고하셔서 다시 실행하세요.'.format(OPT.command[0]))
        # parser.print_help()
        return 1

    # 데이터베이스 초기화
    LOGGER.debug('sqlite file is {}'.format(os.path.abspath(SQLITE_FILE)))
    engine = sqlalchemy.create_engine('sqlite:///' + SQLITE_FILE)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    return 0


def user_confirm(total, album_name=''):
    cond = ''
    if OPT.cmd == 'all':
        cond = '전체 사진 {:,}장'.format(total)
    elif OPT.cmd == 'sync':
        cond = '신규 사진 동기화'
    elif OPT.cmd in ['taken', 'posted']:
        if OPT.stdt == OPT.eddt:
            cond = '{}-{}-{}일에 {}한 사진 {:,}장'.format(
                OPT.stdt[:4], OPT.stdt[4:6], OPT.stdt[6:8],
                '촬영' if OPT.cmd == 'taken' else '업로드', total)
        else:
            cond = '{}-{}-{}부터 {}-{}-{}까지 {}한 사진 {:,}장'.format(
                OPT.stdt[:4], OPT.stdt[4:6], OPT.stdt[6:8], OPT.eddt[:4], OPT.eddt[4:6], OPT.eddt[6:8],
                '촬영' if OPT.cmd == 'taken' else '업로드', total)
    elif OPT.cmd == 'album':
        cond = '[{}] 앨범 사진 {:,}장'.format(album_name, total)

    LOGGER.info('사 용 자: {}({})'.format(OPT.username, OPT.fullname))
    LOGGER.info('다운로드: {}'.format(cond))
    LOGGER.info('저장위치: {}'.format(os.path.abspath(OPT.download_dir)))
    if album_name:
        if OPT.subdir_rule:
            OPT.subdir_rule = os.path.join(get_safe_filename(album_name), OPT.subdir_rule)
        else:
            OPT.subdir_rule = get_safe_filename(album_name)

    if OPT.subdir_rule:
        LOGGER.info('하위폴더: {}'.format(OPT.subdir_rule))
    LOGGER.info('파 일 명: {}.jpg (확장자는 원래 파일대로 유지)'.format(OPT.naming_rule))

    return OPT.yes_anyway is True or input('\n자, 모든 준비가 됐습니다. 시작해볼까요? [Y/N]: ').lower() == 'y'


def flickr_auth():
    global OPT, FLICKR, LOGGER

    FLICKR = flickrapi.FlickrAPI(OPT.api_key, OPT.secret_key, store_token=True, format='parsed-json')
    if not FLICKR.token_valid(perms='read'):
        # Get new OAuth credentials
        FLICKR.get_request_token(oauth_callback='oob')
        url = FLICKR.auth_url(perms='read')
        LOGGER.info("\n최초 실행 시 플리커 인증이 필요합니다. 다음 주소를 브라우저에 넣고 플리커 인증을 수행해주세요:")
        LOGGER.info(url)
        token = input("인증 완료 후 생성된 코드를 이곳에 입력해주세요: ")
        FLICKR.get_access_token(token)
        LOGGER.info('인증이 정상적으로 완료되었습니다.\n')

    # OAuth 인증 완료 후 토큰에 저장되어 있는 사용자 정보를 읽어보자
    OPT.user_id = FLICKR.token_cache.token.user_nsid
    OPT.username = FLICKR.token_cache.token.username
    OPT.fullname = FLICKR.token_cache.token.fullname
    return 0


def flickr_status():
    user = FLICKR.people.getInfo(user_id=OPT.user_id)
    LOGGER.info('[사용자 정보]')
    LOGGER.info('  Flickr Username: {}({})'.format(OPT.username, OPT.fullname))
    LOGGER.info('  Flickr Id: {}'.format(OPT.user_id))
    LOGGER.info('  Flickr URL: {}'.format(user['person']['profileurl']['_content']))
    LOGGER.info('  전체 사진: {:,}'.format(user['person']['photos']['count']['_content']))
    LOGGER.info('  첫 사진 날짜: {}'.format(user['person']['photos']['firstdatetaken']['_content']))
    LOGGER.info('  첫 업로드 날짜: {:%Y-%m-%d %H:%M:%S}'
                .format(datetime.datetime.fromtimestamp(int(user['person']['photos']['firstdate']['_content']))))

    albums = FLICKR.photosets.getList(user_id=OPT.user_id)
    LOGGER.info('\n[앨범 정보]')
    LOGGER.info('앨범 아이디          사진  비디오  앨범이름                                 ')
    for album in albums['photosets']['photoset']:
        LOGGER.info(
            "{:17}  {:>6}  {:>6}  {:40}".format(
                album['id'], album['photos'], album['videos'], album['title']['_content']))

    LOGGER.info("\n** 사진을 다운 받으려면 all, album , taken, posted 등의 명령을 이용하세요.")
    LOGGER.info("** 더 자세한 정보는 -h 옵션을 이용하여 확인해보세요.")
    return 0


def flickr_photo(photo_id):
    # db에 저장된 정보를 우선 사용하고 없으면 flickr api를 이용해 수집한다.
    db_photo = session.query(FlickrPhoto).get(photo_id)
    if not db_photo:
        info = {'photo_id': photo_id, 'hash': ''}

        # getInfo 메서드로 촬영일시, 업로드일시, 마지막 수정시간을 얻는다.
        ext = FLICKR.photos.getInfo(photo_id=photo_id)
        info['date_posted'] = datetime.datetime.fromtimestamp(int(ext['photo']['dates']['posted']))
        info['date_lastupdate'] = datetime.datetime.fromtimestamp(int(ext['photo']['dates']['lastupdate']))
        info['date_taken'] = datetime.datetime.strptime(ext['photo']['dates']['taken'], "%Y-%m-%d %H:%M:%S")

        # exif 정보에서 카메라 모델명을 얻는다.
        exif = FLICKR.photos.getExif(photo_id=photo_id)
        info['model'] = next((item['raw']['_content'] for item in exif['photo']['exif']
                              if item['tag'] == 'Model'), 'Unknown')

        # getSize 메서드로 가장 큰 사진의 url을 얻는다.
        # 'Original', 'Large' 등의 태그는 없을 수도 있어서 픽셀크기 비교로 찾는다.
        biggest_size = 0
        sizes = FLICKR.photos.getSizes(photo_id=photo_id)
        for size in sizes['sizes']['size']:
            this_size = int(size['width']) * int(size['height'])
            if this_size > biggest_size:
                info['url'] = size['source']
                info['width'] = int(size['width'])
                info['height'] = int(size['height'])
                biggest_size = this_size

        db_photo = FlickrPhoto(**info)
        session.add(db_photo)
        session.commit()
    return db_photo


def flickr_download():
    seq = 0
    page = 0
    last_skip = [0 for i in range(SKIP_CHECK_CNT)]  # 100회 연속 이미 sync한 파일이라면 sync 완료 처리

    while True:
        page += 1
        if OPT.cmd in ['all', 'sync']:
            rsp = FLICKR.photos.search(user_id=OPT.user_id, per_page=500, page=page)
            photos = rsp['photos']
        elif OPT.cmd == 'taken':
            rsp = FLICKR.photos.search(user_id=OPT.user_id, per_page=500, page=page,
                                       min_taken_date=OPT.stdt_stamp, max_taken_date=OPT.eddt_stamp)
            photos = rsp['photos']
        elif OPT.cmd == 'posted':
            rsp = FLICKR.photos.search(user_id=OPT.user_id, per_page=500, page=page,
                                       min_upload_date=OPT.stdt_stamp, max_upload_date=OPT.eddt_stamp)
            photos = rsp['photos']
        elif OPT.cmd == 'album':
            rsp = FLICKR.photosets.getPhotos(photoset_id=OPT.album_id, user_id=OPT.user_id, per_page=500, page=page)
            photos = rsp['photoset']

        total = int(photos['total'])
        pages = int(photos['pages'])
        title = photos['title'] if 'title' in photos else ''

        if total == 0:
            LOGGER.info('조건에 맞는 사진이 하나도 없습니다.')
            return 3

        if OPT.run_count == 1:
            if not user_confirm(total, title):
                return 4
            else:
                LOGGER.info('\n')

        for photo in photos['photo']:
            seq += 1
            db_photo = flickr_photo(photo['id'])
            localfile, action, files = make_local_filename(db_photo)
            LOGGER.debug("({}/{}) photo {} {}".format(seq, total, localfile, db_photo.__dict__))

            if action in ['Down', 'Comp']:
                # 다운로드 디렉토리를 먼저 생성해둔다.
                if not os.path.exists(os.path.dirname(localfile)):
                    os.makedirs(os.path.dirname(localfile))

                if OPT.cmd == 'sync':
                    LOGGER.info("새로운 사진번호 {}을 다운받습니다. 파일명: {}".format(db_photo.photo_id, localfile))
                else:
                    LOGGER.info("({}/{}) 사진번호 {}을 다운받습니다. 파일명: {}".format(
                        seq, total, db_photo.photo_id, localfile))

                # 다운로드 오류 감지를 위해 임시파일로 다운받은 후에 정상 파일명으로 변경한다.
                urlretrieve(db_photo.url, localfile + '.part', show_progressbar)
                os.rename(localfile + '.part', localfile)

                # 파일의 atime, mtime을 사진 찍은 시간으로 변경한다.
                taken_time = db_photo.date_taken.timestamp()
                os.utime(localfile, (taken_time, taken_time))

                # 이중 다운로드 방지를 위해 해시값을 기록해 둔다.
                db_photo.hash = md5_checksum(localfile)
                session.commit()

                # 이미 다운로드 받은 파일과 해시 값을 비교하여 새로 받은 파일은 삭제한다.
                if action == 'Comp':
                    for file in files:
                        if db_photo.hash == md5_checksum(file):
                            os.unlink(localfile)
                            LOGGER.info("  주의! 방금 받은 사진({})은 이미 존재하는 사진({})과 동일하므로 삭제합니다.".format(
                                os.path.basename(localfile), os.path.basename(file)))
                            break

            elif action in ['Skip']:
                last_skip.pop(0)
                last_skip.append(1)
                if OPT.cmd != 'sync':
                    LOGGER.info("({}/{}) 사진번호 {}는 이미 다운로드 했습니다. 파일명: {}".format(
                        seq, total, db_photo.photo_id, localfile))

        # 마지막 페이지 또는 일정 건수가 연속으로 이미 다운로드한 사진이라면 sync 완료 처리
        if page == pages or (OPT.cmd == 'sync' and sum(last_skip) == SKIP_CHECK_CNT):
            break

    OPT.cmd != 'sync' and LOGGER.info('다운로드 완료했습니다. 잘 받아졌는지 확인해보세요~')
    return 0


def md5_checksum(filespec):
    return hashlib.md5(open(filespec, 'rb').read()).hexdigest()


def show_progressbar(blocknum, blocksize, totalsize):
    readsofar = blocknum * blocksize
    if totalsize > 0:
        percent = min(readsofar / totalsize, 1)
        s = '\r          [{:20}] {:7.2%} ({:.2f}M/{:.2f}M)'.format(
            '#' * int(percent * 20),
            percent,
            readsofar / 1000000,
            totalsize / 1000000
        )
        sys.stderr.write(s)
        if readsofar >= totalsize:  # near the end
            sys.stderr.write('\r')
    else:  # total size is unknown
        sys.stderr.write('read %d\n' % (readsofar,))


def get_safe_filename(filename):
    """
     파일명으로 사용할 수 없는 문자(\/:*?"'<>|)를 안전한 문자로 변경한다. 디렉토리 구분자를 OS에 맞게 구분한다.
    :param filename: 변환 할 파일명
    :return: 안전하게 변환 한 파일명
    """
    safe_filename = filename.replace(':\\', '__DRIVE_DIVIDER__')
    safe_filename = re.sub(r'[:\?\*\"\'<>\|]', '_', safe_filename)
    safe_filename = os.path.join(*safe_filename.split('/'))
    safe_filename = os.path.join(*safe_filename.split('\\'))
    safe_filename = safe_filename.replace('__DRIVE_DIVIDER__', ':\\')
    return safe_filename


def make_local_filename(photo):
    namings = []
    ext = os.path.splitext(photo.url)[1]

    for s in [OPT.subdir_rule, OPT.naming_rule]:
        s = s.replace('YYYY', '{:%Y}'.format(photo.date_taken))
        s = s.replace('MM', '{:%m}'.format(photo.date_taken))
        s = s.replace('DD', '{:%d}'.format(photo.date_taken))
        s = s.replace('hh', '{:%H}'.format(photo.date_taken))
        s = s.replace('mm', '{:%M}'.format(photo.date_taken))
        s = s.replace('ss', '{:%S}'.format(photo.date_taken))
        s = s.replace('camera', '{}'.format(photo.model))
        s = s.replace('photo_id', '{}'.format(photo.photo_id))
        namings.append(s)

    # 사용자가 지정한 규칙에 따라 파일명을 만든다.
    # 이름이 같은 파일이 존재하는 경우 아래 순서에 따라 처리한다.
    # 1) db에 hash 값이 이미 있고 기존 파일과 hash가 동일하면 skip 처리한다.
    # 2) db에 hash 값이 이미 있고 기존 파일과 hash가 다르면 순번을 붙인 파일명을 사용한다.
    # 3) db에 hash 값이 없어서 중복 여부 확인이 불가하면 순번을 붙인 파일명으로 다운받은 후에 hash를 비교하여
    #    중복 여부를 점검하고 불필요 파일은 삭제한다.
    comp_files = []
    for i in range(1, 99):
        if i == 1:
            filespec = os.path.join(OPT.download_dir, namings[0], namings[1] + ext)
        else:
            filespec = os.path.join(OPT.download_dir, namings[0], namings[1] + '-' + str(i) + ext)

        if not os.path.exists(filespec):
            if i == 1 or photo.hash:
                return filespec, 'Down', None
            else:
                return filespec, 'Comp', comp_files
        elif os.path.exists(filespec) and md5_checksum(filespec) == photo.hash:
            return filespec, 'Skip', None
        if i == 99:
            LOGGER.error('중복된 파일이 너무 많습니다. 기존 파일을 지운 후에 재시도 하시기 바랍니다.')
            LOGGER.error('사진번호: {}  파일위치: {}'.format(photo.photo_id, filespec))
            sys.exit(3)

        comp_files.append(filespec)

    return


def main():
    # 초기화
    rtn = _init()
    if rtn > 0:
        return rtn

    # 플리커 인증: 비공개 사진 접근을 위해 OAuth를 수행하고 flickr id를 획득한다.
    rtn = flickr_auth()
    if rtn > 0:
        return rtn

    try:
        OPT.run_count = 1
        if OPT.cmd == 'status':
            return flickr_status()
        elif OPT.cmd in ['all', 'album', 'taken', 'posted']:
            return flickr_download()
        elif OPT.cmd == 'sync':
            while True:
                rtn = flickr_download()
                if rtn > 0:
                    return rtn
                time.sleep(SYNC_SLEEP)
                OPT.run_count += 1

    except KeyboardInterrupt:
        print('\n\n강제 종료합니다. 다운로드 중이던 사진은 확장자가 .part 상태로 남을 수 있습니다.', file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())
