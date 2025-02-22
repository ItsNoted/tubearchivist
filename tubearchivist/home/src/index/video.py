"""
functionality:
- get metadata from youtube for a video
- index and update in es
"""

import json
import os
import re
from datetime import datetime

import requests
from home.src.es.connect import ElasticWrap
from home.src.index import channel as ta_channel
from home.src.index.generic import YouTubeItem
from home.src.ta.helper import DurationConverter, clean_string
from ryd_client import ryd_client


class YoutubeSubtitle:
    """handle video subtitle functionality"""

    def __init__(self, video):
        self.video = video
        self.languages = False

    def sub_conf_parse(self):
        """add additional conf values to self"""
        languages_raw = self.video.config["downloads"]["subtitle"]
        if languages_raw:
            self.languages = [i.strip() for i in languages_raw.split(",")]

    def get_subtitles(self):
        """check what to do"""
        self.sub_conf_parse()
        if not self.languages:
            # no subtitles
            return False

        relevant_subtitles = []
        for lang in self.languages:
            user_sub = self.get_user_subtitles(lang)
            if user_sub:
                relevant_subtitles.append(user_sub)
                continue

            if self.video.config["downloads"]["subtitle_source"] == "auto":
                auto_cap = self.get_auto_caption(lang)
                if auto_cap:
                    relevant_subtitles.append(auto_cap)

        return relevant_subtitles

    def get_auto_caption(self, lang):
        """get auto_caption subtitles"""
        print(f"{self.video.youtube_id}-{lang}: get auto generated subtitles")
        all_subtitles = self.video.youtube_meta.get("automatic_captions")

        if not all_subtitles:
            return False

        video_media_url = self.video.json_data["media_url"]
        media_url = video_media_url.replace(".mp4", f"-{lang}.vtt")
        all_formats = all_subtitles.get(lang)
        if not all_formats:
            return False

        subtitle = [i for i in all_formats if i["ext"] == "vtt"][0]
        subtitle.update(
            {"lang": lang, "source": "auto", "media_url": media_url}
        )

        return subtitle

    def _normalize_lang(self):
        """normalize country specific language keys"""
        all_subtitles = self.video.youtube_meta.get("subtitles")
        if not all_subtitles:
            return False

        all_keys = list(all_subtitles.keys())
        for key in all_keys:
            lang = key.split("-")[0]
            old = all_subtitles.pop(key)
            if lang == "live_chat":
                continue
            all_subtitles[lang] = old

        return all_subtitles

    def get_user_subtitles(self, lang):
        """get subtitles uploaded from channel owner"""
        print(f"{self.video.youtube_id}-{lang}: get user uploaded subtitles")
        all_subtitles = self._normalize_lang()
        if not all_subtitles:
            return False

        video_media_url = self.video.json_data["media_url"]
        media_url = video_media_url.replace(".mp4", f"-{lang}.vtt")
        all_formats = all_subtitles.get(lang)
        if not all_formats:
            # no user subtitles found
            return False

        subtitle = [i for i in all_formats if i["ext"] == "vtt"][0]
        subtitle.update(
            {"lang": lang, "source": "user", "media_url": media_url}
        )

        return subtitle

    def download_subtitles(self, relevant_subtitles):
        """download subtitle files to archive"""
        videos_base = self.video.config["application"]["videos"]
        for subtitle in relevant_subtitles:
            dest_path = os.path.join(videos_base, subtitle["media_url"])
            source = subtitle["source"]
            response = requests.get(subtitle["url"])
            if not response.ok:
                print(f"{self.video.youtube_id}: failed to download subtitle")
                continue

            parser = SubtitleParser(response.text, subtitle.get("lang"))
            parser.process()
            subtitle_str = parser.get_subtitle_str()
            self._write_subtitle_file(dest_path, subtitle_str)
            if self.video.config["downloads"]["subtitle_index"]:
                query_str = parser.create_bulk_import(self.video, source)
                self._index_subtitle(query_str)

    @staticmethod
    def _write_subtitle_file(dest_path, subtitle_str):
        """write subtitle file to disk"""
        # create folder here for first video of channel
        os.makedirs(os.path.split(dest_path)[0], exist_ok=True)
        with open(dest_path, "w", encoding="utf-8") as subfile:
            subfile.write(subtitle_str)

    @staticmethod
    def _index_subtitle(query_str):
        """send subtitle to es for indexing"""
        _, _ = ElasticWrap("_bulk").post(data=query_str, ndjson=True)


class SubtitleParser:
    """parse subtitle str from youtube"""

    time_reg = r"^([0-9]{2}:?){3}\.[0-9]{3} --> ([0-9]{2}:?){3}\.[0-9]{3}"
    stamp_reg = r"<([0-9]{2}:?){3}\.[0-9]{3}>"
    tag_reg = r"</?c>"

    def __init__(self, subtitle_str, lang):
        self.subtitle_str = subtitle_str
        self.lang = lang
        self.header = False
        self.parsed_cue_list = False
        self.all_text_lines = False
        self.matched = False

    def process(self):
        """collection to process subtitle string"""
        self._parse_cues()
        self._match_text_lines()
        self._add_id()
        self._timestamp_check()

    def _parse_cues(self):
        """split into cues"""
        all_cues = self.subtitle_str.replace("\n \n", "\n").split("\n\n")
        self.header = all_cues[0]
        self.all_text_lines = []
        self.parsed_cue_list = [self._cue_cleaner(i) for i in all_cues[1:]]

    def _cue_cleaner(self, cue):
        """parse single cue"""
        all_lines = cue.split("\n")
        cue_dict = {"lines": []}

        for line in all_lines:
            if re.match(self.time_reg, line):
                clean = re.search(self.time_reg, line).group()
                start, end = clean.split(" --> ")
                cue_dict.update({"start": start, "end": end})
            else:
                clean = re.sub(self.stamp_reg, "", line)
                clean = re.sub(self.tag_reg, "", clean)
                cue_dict["lines"].append(clean)
                if clean.strip() and clean not in self.all_text_lines[-4:]:
                    # remove immediate duplicates
                    self.all_text_lines.append(clean)

        return cue_dict

    def _match_text_lines(self):
        """match unique text lines with timestamps"""

        self.matched = []

        while self.all_text_lines:
            check = self.all_text_lines[0]
            matches = [i for i in self.parsed_cue_list if check in i["lines"]]
            new_cue = matches[-1]
            new_cue["start"] = matches[0]["start"]

            for line in new_cue["lines"]:
                try:
                    self.all_text_lines.remove(line)
                except ValueError:
                    continue

            self.matched.append(new_cue)

    def _timestamp_check(self):
        """check if end timestamp is bigger than start timestamp"""
        for idx, cue in enumerate(self.matched):
            # this
            end = int(re.sub("[^0-9]", "", cue.get("end")))
            # next
            try:
                next_cue = self.matched[idx + 1]
            except IndexError:
                continue

            start_next = int(re.sub("[^0-9]", "", next_cue.get("start")))
            if end > start_next:
                self.matched[idx]["end"] = next_cue.get("start")

    def _add_id(self):
        """add id to matched cues"""
        for idx, _ in enumerate(self.matched):
            self.matched[idx]["id"] = idx + 1

    def get_subtitle_str(self):
        """stitch cues and return processed new string"""
        new_subtitle_str = self.header + "\n\n"

        for cue in self.matched:
            timestamp = f"{cue.get('start')} --> {cue.get('end')}"
            lines = "\n".join(cue.get("lines"))
            cue_text = f"{cue.get('id')}\n{timestamp}\n{lines}\n\n"
            new_subtitle_str = new_subtitle_str + cue_text

        return new_subtitle_str

    def create_bulk_import(self, video, source):
        """process matched for es import"""
        bulk_list = []
        channel = video.json_data.get("channel")

        document = {
            "youtube_id": video.youtube_id,
            "title": video.json_data.get("title"),
            "subtitle_channel": channel.get("channel_name"),
            "subtitle_channel_id": channel.get("channel_id"),
            "subtitle_last_refresh": int(datetime.now().strftime("%s")),
            "subtitle_lang": self.lang,
            "subtitle_source": source,
        }

        for match in self.matched:
            match_id = match.get("id")
            document_id = f"{video.youtube_id}-{self.lang}-{match_id}"
            action = {"index": {"_index": "ta_subtitle", "_id": document_id}}
            document.update(
                {
                    "subtitle_fragment_id": document_id,
                    "subtitle_start": match.get("start"),
                    "subtitle_end": match.get("end"),
                    "subtitle_index": match_id,
                    "subtitle_line": " ".join(match.get("lines")),
                }
            )
            bulk_list.append(json.dumps(action))
            bulk_list.append(json.dumps(document))

        bulk_list.append("\n")
        query_str = "\n".join(bulk_list)

        return query_str


class YoutubeVideo(YouTubeItem, YoutubeSubtitle):
    """represents a single youtube video"""

    es_path = False
    index_name = "ta_video"
    yt_base = "https://www.youtube.com/watch?v="

    def __init__(self, youtube_id):
        super().__init__(youtube_id)
        self.channel_id = False
        self.es_path = f"{self.index_name}/_doc/{youtube_id}"

    def build_json(self):
        """build json dict of video"""
        self.get_from_youtube()
        if not self.youtube_meta:
            return

        self._process_youtube_meta()
        self._add_channel()
        self._add_stats()
        self.add_file_path()
        self.add_player()
        self._check_subtitles()
        if self.config["downloads"]["integrate_ryd"]:
            self._get_ryd_stats()

        return

    def _process_youtube_meta(self):
        """extract relevant fields from youtube"""
        # extract
        self.channel_id = self.youtube_meta["channel_id"]
        upload_date = self.youtube_meta["upload_date"]
        upload_date_time = datetime.strptime(upload_date, "%Y%m%d")
        published = upload_date_time.strftime("%Y-%m-%d")
        last_refresh = int(datetime.now().strftime("%s"))
        # build json_data basics
        self.json_data = {
            "title": self.youtube_meta["title"],
            "description": self.youtube_meta["description"],
            "category": self.youtube_meta["categories"],
            "vid_thumb_url": self.youtube_meta["thumbnail"],
            "tags": self.youtube_meta["tags"],
            "published": published,
            "vid_last_refresh": last_refresh,
            "date_downloaded": last_refresh,
            "youtube_id": self.youtube_id,
            "active": True,
        }

    def _add_channel(self):
        """add channel dict to video json_data"""
        channel = ta_channel.YoutubeChannel(self.channel_id)
        channel.build_json(upload=True)
        self.json_data.update({"channel": channel.json_data})

    def _add_stats(self):
        """add stats dicst to json_data"""
        # likes
        like_count = self.youtube_meta.get("like_count", 0)
        dislike_count = self.youtube_meta.get("dislike_count", 0)
        self.json_data.update(
            {
                "stats": {
                    "view_count": self.youtube_meta["view_count"],
                    "like_count": like_count,
                    "dislike_count": dislike_count,
                    "average_rating": self.youtube_meta["average_rating"],
                }
            }
        )

    def build_dl_cache_path(self):
        """find video path in dl cache"""
        cache_dir = self.app_conf["cache_dir"]
        cache_path = f"{cache_dir}/download/"
        all_cached = os.listdir(cache_path)
        for file_cached in all_cached:
            if self.youtube_id in file_cached:
                vid_path = os.path.join(cache_path, file_cached)
                return vid_path

        raise FileNotFoundError

    def add_player(self):
        """add player information for new videos"""
        try:
            # when indexing from download task
            vid_path = self.build_dl_cache_path()
        except FileNotFoundError as err:
            # when reindexing needs to handle title rename
            channel = os.path.split(self.json_data["media_url"])[0]
            channel_dir = os.path.join(self.app_conf["videos"], channel)
            all_files = os.listdir(channel_dir)
            for file in all_files:
                if self.youtube_id in file:
                    vid_path = os.path.join(channel_dir, file)
                    break
            else:
                raise FileNotFoundError("could not find video file") from err

        duration_handler = DurationConverter()
        duration = duration_handler.get_sec(vid_path)
        duration_str = duration_handler.get_str(duration)
        self.json_data.update(
            {
                "player": {
                    "watched": False,
                    "duration": duration,
                    "duration_str": duration_str,
                }
            }
        )

    def add_file_path(self):
        """build media_url for where file will be located"""
        channel_name = self.json_data["channel"]["channel_name"]
        clean_channel_name = clean_string(channel_name)
        if len(clean_channel_name) <= 3:
            # fall back to channel id
            clean_channel_name = self.json_data["channel"]["channel_id"]

        timestamp = self.json_data["published"].replace("-", "")
        youtube_id = self.json_data["youtube_id"]
        title = self.json_data["title"]
        clean_title = clean_string(title)
        filename = f"{timestamp}_{youtube_id}_{clean_title}.mp4"
        media_url = os.path.join(clean_channel_name, filename)
        self.json_data["media_url"] = media_url

    def delete_media_file(self):
        """delete video file, meta data"""
        self.get_from_es()
        video_base = self.app_conf["videos"]
        to_del = [self.json_data.get("media_url")]

        all_subtitles = self.json_data.get("subtitles")
        if all_subtitles:
            to_del = to_del + [i.get("media_url") for i in all_subtitles]

        for media_url in to_del:
            file_path = os.path.join(video_base, media_url)
            os.remove(file_path)

        self.del_in_es()
        self.delete_subtitles()

    def _get_ryd_stats(self):
        """get optional stats from returnyoutubedislikeapi.com"""
        try:
            print(f"{self.youtube_id}: get ryd stats")
            result = ryd_client.get(self.youtube_id)
        except requests.exceptions.ConnectionError:
            print(f"{self.youtube_id}: failed to query ryd api, skipping")
            return False

        if result["status"] == 404:
            return False

        dislikes = {
            "dislike_count": result["dislikes"],
            "average_rating": result["rating"],
        }
        self.json_data["stats"].update(dislikes)

        return True

    def _check_subtitles(self):
        """optionally add subtitles"""
        handler = YoutubeSubtitle(self)
        subtitles = handler.get_subtitles()
        if subtitles:
            self.json_data["subtitles"] = subtitles
            handler.download_subtitles(relevant_subtitles=subtitles)

    def delete_subtitles(self):
        """delete indexed subtitles"""
        data = {"query": {"term": {"youtube_id": {"value": self.youtube_id}}}}
        _, _ = ElasticWrap("ta_subtitle/_delete_by_query").post(data=data)


def index_new_video(youtube_id):
    """combined classes to create new video in index"""
    video = YoutubeVideo(youtube_id)
    video.build_json()
    if not video.json_data:
        raise ValueError("failed to get metadata for " + youtube_id)

    video.upload_to_es()
    return video.json_data
