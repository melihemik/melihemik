import datetime
import os
import time
import hashlib
import json
import subprocess
import urllib.request
import urllib.error

try:
    from lxml import etree
except ImportError:
    import xml.etree.ElementTree as etree


def get_access_token():
    token = os.environ.get('ACCESS_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token
    try:
        token = subprocess.check_output(['gh', 'auth', 'token'], stderr=subprocess.DEVNULL).decode().strip()
        if token:
            return token
    except Exception:
        pass
    return None


ACCESS_TOKEN = get_access_token()
USER_NAME = os.environ.get('USER_NAME', 'melihemik')
HEADERS = {
    'Authorization': f'Bearer {ACCESS_TOKEN}',
    'Content-Type': 'application/json',
    'User-Agent': 'melihemik-readme-bot'
} if ACCESS_TOKEN else {}

QUERY_COUNT = 0


def simple_request(query, variables=None, retries=3):
    global QUERY_COUNT
    QUERY_COUNT += 1
    if not ACCESS_TOKEN:
        raise Exception("ACCESS_TOKEN or GITHUB_TOKEN is required for API queries")

    data = json.dumps({'query': query, 'variables': variables or {}}).encode('utf-8')
    req = urllib.request.Request('https://api.github.com/graphql', data=data, headers=HEADERS)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = json.loads(response.read().decode('utf-8'))
                if 'errors' in body and not body.get('data'):
                    raise Exception(f"GraphQL error: {body['errors']}")
                return body
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise e
            time.sleep(1 + attempt * 2)


def get_age_diff(birthday):
    today = datetime.datetime.today()
    years = today.year - birthday.year
    months = today.month - birthday.month
    days = today.day - birthday.day
    if days < 0:
        months -= 1
        prev_month = (today.month - 1) or 12
        prev_year = today.year if prev_month != 12 else today.year - 1
        import calendar
        days += calendar.monthrange(prev_year, prev_month)[1]
    if months < 0:
        years -= 1
        months += 12

    class AgeDiff:
        pass

    d = AgeDiff()
    d.years = years
    d.months = months
    d.days = days
    return d


def format_plural(unit):
    return 's' if unit != 1 else ''


def daily_readme(birthday):
    diff = get_age_diff(birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂 ' if (diff.months == 0 and diff.days == 0) else ''
    )


def user_getter(username):
    query = '''
    query($login: String!) {
        user(login: $login) {
            id
            createdAt
            followers {
                totalCount
            }
        }
    }'''
    res = simple_request(query, {'login': username})
    user_data = res['data']['user']
    return user_data['id'], user_data['followers']['totalCount']


def get_all_repositories(username):
    query = '''
    query ($login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
                totalCount
                edges {
                    node {
                        nameWithOwner
                        stargazerCount
                        isFork
                        owner {
                            login
                        }
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    cursor = None
    all_edges = []
    while True:
        res = simple_request(query, {'login': username, 'cursor': cursor})
        repo_data = res.get('data', {}).get('user', {}).get('repositories', {})
        edges = repo_data.get('edges', []) or []
        all_edges.extend(edges)
        if not repo_data.get('pageInfo', {}).get('hasNextPage'):
            break
        cursor = repo_data['pageInfo']['endCursor']
    return all_edges


def recursive_loc(owner, repo_name, author_id, cursor=None, addition_total=0, deletion_total=0, my_commits=0):
    query = '''
    query ($repo_name: String!, $owner: String!, $author_id: ID!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, author: {id: $author_id}, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'author_id': author_id, 'cursor': cursor}
    try:
        res = simple_request(query, variables)
        repo_obj = res.get('data', {}).get('repository')
        if not repo_obj or not repo_obj.get('defaultBranchRef') or not repo_obj['defaultBranchRef'].get('target'):
            return addition_total, deletion_total, my_commits

        history = repo_obj['defaultBranchRef']['target'].get('history', {})
        edges = history.get('edges', []) or []
        for item in edges:
            if not item or not isinstance(item, dict):
                continue
            commit_node = item.get('node')
            if not commit_node or not isinstance(commit_node, dict):
                continue
            author = commit_node.get('author')
            if author and author.get('user', {}).get('id') == author_id:
                my_commits += 1
                addition_total += commit_node.get('additions', 0)
                deletion_total += commit_node.get('deletions', 0)

        if history.get('pageInfo', {}).get('hasNextPage') and history['pageInfo'].get('endCursor'):
            return recursive_loc(owner, repo_name, author_id, history['pageInfo']['endCursor'], addition_total, deletion_total, my_commits)
        return addition_total, deletion_total, my_commits
    except Exception as e:
        print(f"Warning: recursive_loc failed for {owner}/{repo_name}: {e}")
        return addition_total, deletion_total, my_commits


def load_cache(filename, comment_size=7):
    comments = []
    cache_dict = {}
    if not os.path.exists(filename):
        return comments, cache_dict

    with open(filename, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    comments = lines[:comment_size]
    for line in lines[comment_size:]:
        parts = line.strip().split()
        if len(parts) >= 5:
            try:
                cache_dict[parts[0]] = [int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])]
            except ValueError:
                continue
    return comments, cache_dict


def save_cache(filename, comments, cache_dict, comment_size=7):
    os.makedirs(os.path.dirname(filename) or '.', exist_ok=True)
    if not comments and comment_size > 0:
        for _ in range(comment_size):
            comments.append('096aefba1269497a7092bf7b6cfb76e8ebf1a67931360bd8cd3644e2fc2ca22c 0 0 0 0\n')

    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(comments)
        for repo_hash, values in cache_dict.items():
            f.write(f"{repo_hash} {values[0]} {values[1]} {values[2]} {values[3]}\n")


def update_stats_and_cache(edges, author_id):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    comments, cache_dict = load_cache(filename, 7)

    owner_repos_count = 0
    owner_stars_count = 0
    contributed_repos_count = len(edges)

    for edge in edges:
        if not edge or not edge.get('node'):
            continue
        node = edge['node']
        name_with_owner = node.get('nameWithOwner')
        if not name_with_owner:
            continue

        is_owner = (node.get('owner', {}).get('login') == USER_NAME)
        if is_owner:
            owner_repos_count += 1
            owner_stars_count += node.get('stargazerCount', 0)

        repo_hash = hashlib.sha256(name_with_owner.encode('utf-8')).hexdigest()
        default_branch = node.get('defaultBranchRef')
        history_total = 0
        if default_branch and default_branch.get('target') and default_branch['target'].get('history'):
            history_total = default_branch['target']['history'].get('totalCount', 0)

        # Check if cache is valid for this repo
        cached_entry = cache_dict.get(repo_hash)
        if cached_entry and cached_entry[0] == history_total and (cached_entry[1] > 0 or history_total == 0):
            # No changes, keep cached value
            pass
        else:
            # Commits changed or newly encountered repo
            owner, repo_name = name_with_owner.split('/')
            additions, deletions, my_commits = recursive_loc(owner, repo_name, author_id)
            cache_dict[repo_hash] = [history_total, my_commits, additions, deletions]

    save_cache(filename, comments, cache_dict, 7)

    total_commits = sum(v[1] for v in cache_dict.values())
    total_loc_add = sum(v[2] for v in cache_dict.values())
    total_loc_del = sum(v[3] for v in cache_dict.values())
    net_loc = total_loc_add - total_loc_del

    return {
        'owner_repos': owner_repos_count,
        'owner_stars': owner_stars_count,
        'contrib_repos': contributed_repos_count,
        'commits': total_commits,
        'loc_add': total_loc_add,
        'loc_del': total_loc_del,
        'net_loc': net_loc
    }


def add_archive():
    if not os.path.exists('cache/repository_archive.txt'):
        return [0, 0, 0, 0, 0]
    with open('cache/repository_archive.txt', 'r', encoding='utf-8') as f:
        data = f.readlines()
    old_data = data
    data = data[7:max(7, len(data) - 3)]
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        parts = line.split()
        if len(parts) >= 4:
            my_commits = parts[2]
            loc = parts[3:]
            if len(loc) >= 1 and loc[0].isdigit():
                added_loc += int(loc[0])
            if len(loc) >= 2 and loc[1].isdigit():
                deleted_loc += int(loc[1])
            if my_commits.isdigit():
                added_commits += int(my_commits)
    if old_data and len(old_data[-1].split()) >= 5:
        last_part = old_data[-1].split()[4].rstrip(';.,')
        if last_part.isdigit():
            added_commits += int(last_part)
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]


def find_and_replace(root, element_id, new_text):
    for elem in root.iter():
        if elem.attrib.get('id') == element_id:
            elem.text = str(new_text)
            return


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_val):
    tree = etree.parse(filename)
    root = tree.getroot()
    find_and_replace(root, 'age_data', age_data)
    find_and_replace(root, 'commit_data', f"{'{:,}'.format(commit_data)}" if isinstance(commit_data, int) else str(commit_data))
    find_and_replace(root, 'star_data', f"{'{:,}'.format(star_data)}" if isinstance(star_data, int) else str(star_data))
    find_and_replace(root, 'repo_data', f"{'{:,}'.format(repo_data)}" if isinstance(repo_data, int) else str(repo_data))
    find_and_replace(root, 'contrib_data', f"{'{:,}'.format(contrib_data)}" if isinstance(contrib_data, int) else str(contrib_data))
    find_and_replace(root, 'follower_data', f"{'{:,}'.format(follower_data)}" if isinstance(follower_data, int) else str(follower_data))
    find_and_replace(root, 'loc_data', f"{'{:,}'.format(loc_val)}" if isinstance(loc_val, int) else str(loc_val))
    tree.write(filename, encoding='utf-8', xml_declaration=True)


if __name__ == '__main__':
    print('Updating README stats for:', USER_NAME)

    age_data = daily_readme(datetime.datetime(2005, 7, 25))

    if not ACCESS_TOKEN:
        print('No ACCESS_TOKEN / GITHUB_TOKEN found. Updating age only...')
        for filename in ['dark_mode.svg', 'light_mode.svg']:
            if os.path.exists(filename):
                tree = etree.parse(filename)
                root = tree.getroot()
                find_and_replace(root, 'age_data', age_data)
                tree.write(filename, encoding='utf-8', xml_declaration=True)
        print('Age updated successfully:', age_data)
        exit(0)

    start_time = time.time()
    author_id, follower_data = user_getter(USER_NAME)
    edges = get_all_repositories(USER_NAME)

    stats = update_stats_and_cache(edges, author_id)
    archived_data = add_archive()

    total_net_loc = stats['net_loc'] + archived_data[2]
    total_commits = stats['commits'] + archived_data[3]
    total_contrib = stats['contrib_repos'] + archived_data[4]
    owner_repos = stats['owner_repos']
    owner_stars = stats['owner_stars']

    print(f"Stats calculated in {time.time() - start_time:.2f}s:")
    print(f"  Age: {age_data}")
    print(f"  Owner Repos: {owner_repos}")
    print(f"  Owner Stars: {owner_stars}")
    print(f"  Total Contributed: {total_contrib}")
    print(f"  Total Commits: {total_commits:,}")
    print(f"  Followers: {follower_data}")
    print(f"  Lines of Code: {total_net_loc:,}")

    for filename in ['dark_mode.svg', 'light_mode.svg']:
        if os.path.exists(filename):
            svg_overwrite(filename, age_data, total_commits, owner_stars, owner_repos, total_contrib, follower_data, total_net_loc)
            print(f"Updated {filename}")

    print('SVGs updated successfully.')
