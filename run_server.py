import json
import argparse
import torch
import tokenization
import os
import random
import numpy as np
import requests
import logging
import math
import ssl
import best
import copy
from time import time
from flask import Flask, request, jsonify, render_template, redirect
from flask_cors import CORS
from tornado.wsgi import WSGIContainer
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from requests_futures.sessions import FuturesSession
from tqdm import tqdm
from collections import namedtuple

from serve_utils import load_caches, parse_example, get_cached, get_search
from train_query import train_query_encoder
from modeling import BertConfig
from query_encoder import QueryEncoder
from tfidf_doc_ranker import TfidfDocRanker
from run_natkb import check_diff
from pre import SquadExample, convert_questions_to_features
from post import convert_question_features_to_dataloader, get_question_results
from mips_phrase import MIPS
from mips_sent import MIPS_SENT
from eval_utils import normalize_answer, f1_score, exact_match_score, drqa_exact_match_score, drqa_regex_match_score,\
                       drqa_metric_max_over_ground_truths, drqa_normalize

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s', datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


class DenSPIServer(object):
    def __init__(self, args):
        self.args = args
        # IP and Ports
        self.base_ip = args.base_ip
        self.query_port = args.query_port
        self.doc_port = args.doc_port
        self.index_port = args.index_port

        # Saved objects
        self.mips = None

    def load_query_encoder(self, device, args):
        # Configure paths for query encoder serving
        vocab_path = os.path.join(args.metadata_dir, args.vocab_name)
        bert_config_path = os.path.join(
            args.metadata_dir, args.bert_config_name.replace(".json", "") + "_" + args.bert_model_option + ".json"
        )

        # Load pretrained QueryEncoder
        bert_config = BertConfig.from_json_file(bert_config_path)
        model = QueryEncoder(bert_config, use_biobert=args.use_biobert, hard_em=args.hard_em)
        if args.parallel:
            model = torch.nn.DataParallel(model)
        model.to(device)
        state = torch.load(args.query_encoder_path, map_location=device)
        model.load_state_dict(state['model'], strict=False)
        check_diff(model.state_dict(), state['model'])
        tokenizer = tokenization.FullTokenizer(vocab_file=vocab_path, do_lower_case=not args.do_case)

        logger.info('Model loaded from %s' % args.query_encoder_path)
        logger.info('Number of model parameters: {:,}'.format(sum(p.numel() for p in model.parameters())))
        return model, tokenizer

    def get_question_dataloader(self, questions, tokenizer, batch_size):
        question_examples = [SquadExample(qas_id='qs', question_text=q) for q in questions]
        query_features = convert_questions_to_features(
            examples=question_examples,
            tokenizer=tokenizer,
            max_query_length=64
        )
        question_dataloader = convert_question_features_to_dataloader(
            query_features,
            fp16=False, local_rank=-1,
            predict_batch_size=batch_size
        )
        return question_dataloader, question_examples, query_features

    def serve_query_encoder(self, query_port, args):
        device = 'cuda' if args.cuda else 'cpu'
        query_encoder, tokenizer = self.load_query_encoder(device, args)

        # Define query to vector function
        def query2vec(queries):
            question_dataloader, question_examples, query_features = self.get_question_dataloader(
                queries, tokenizer, batch_size=24
            )
            query_encoder.eval()
            question_results = get_question_results(
                question_examples, query_features, question_dataloader, device, query_encoder
            )
            outs = []
            for qr_idx, question_result in enumerate(question_results):
                for ngram in question_result.sparse.keys():
                    question_result.sparse[ngram] = question_result.sparse[ngram].tolist()
                out = (
                    question_result.start.tolist(), question_result.end.tolist(),
                    question_result.sparse, question_result.input_ids
                )
                outs.append(out)
            return outs

        # Serve query encoder
        app = Flask(__name__)
        app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
        CORS(app)

        @app.route('/batch_api', methods=['POST'])
        def batch_api():
            batch_query = json.loads(request.form['query'])
            outs = query2vec(batch_query)
            return jsonify(outs)

        logger.info(f'Starting QueryEncoder server at {self.get_address(query_port)}')
        http_server = HTTPServer(WSGIContainer(app))
        http_server.listen(query_port)
        IOLoop.instance().start()

    def load_phrase_index(self, args, dump_only=False, sent_search=False):
        if self.mips is not None:
            return self.mips

        # Configure paths for index serving
        phrase_dump_dir = os.path.join(args.dump_dir, args.phrase_dir)
        tfidf_dump_dir = os.path.join(args.dump_dir, args.tfidf_dir)
        index_dir = os.path.join(args.dump_dir, args.index_dir)
        index_path = os.path.join(index_dir, args.index_name)
        idx2id_path = os.path.join(index_dir, args.idx2id_name)
        max_norm_path = os.path.join(index_dir, 'max_norm.json')

        # Load mips
        mips_init = MIPS if not sent_search else MIPS_SENT
        mips = mips_init(
            phrase_dump_dir=phrase_dump_dir,
            tfidf_dump_dir=tfidf_dump_dir,
            start_index_path=index_path,
            idx2id_path=idx2id_path,
            max_norm_path=max_norm_path,
            doc_rank_fn={
                'index': self.get_doc_scores, 'top_docs': self.get_top_docs, 'doc_meta': self.get_doc_meta,
                'spvec': self.get_q_spvecs
            },
            cuda=args.cuda, dump_only=dump_only
        )
        return mips

    def serve_phrase_index(self, index_port, args):
        if index_port == '80':
            app = Flask(__name__, static_url_path='/static', static_folder="static",
                template_folder="templates")
            app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
            CORS(app)
            @app.before_request
            def before_request():
                if request.url.startswith('http://'):
                    url = request.url.replace('http://', 'https://', 1)
                    code = 301
                    return redirect(url, code=code)
            http_server = HTTPServer(WSGIContainer(app))
            http_server.listen(index_port)
            IOLoop.instance().start()
            return

        dev_str = '_dev' if args.develop else ''
        args.examples_path = os.path.join(f'static{dev_str}', args.examples_path)
        args.google_examples_path = os.path.join(f'static{dev_str}', args.google_examples_path)
        args.kcw_examples_path = os.path.join(f'static{dev_str}', args.kcw_examples_path)
        args.top100_covid_examples_path = os.path.join(f'static{dev_str}', args.top100_covid_examples_path)
        args.top100_google_examples_path = os.path.join(f'static{dev_str}', args.top100_google_examples_path)
        args.top10_kcw_examples_path = os.path.join(f'static{dev_str}', args.top10_kcw_examples_path)

        # Load mips
        self.mips = self.load_phrase_index(args, sent_search=args.sent_search)
        app = Flask(__name__, static_url_path='/static' + dev_str, static_folder="static" + dev_str,
            template_folder="templates" + dev_str)
        app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
        CORS(app)

        # From serve_utils
        cached_set = load_caches(args)
        index_example_set, search_examples, inverted_examples, query_entity_ids = parse_example(args)

        def batch_search(batch_query, max_answer_length=20, start_top_k=1000, mid_top_k=100, top_k=10, doc_top_k=5,
                         nprobe=64, sparse_weight=0.05, search_strategy='hybrid', aggregate=False):
            t0 = time()
            outs, _ = self.embed_query(batch_query)()
            start = np.concatenate([out[0] for out in outs], 0)
            end = np.concatenate([out[1] for out in outs], 0)
            sparse_uni = [out[2]['1'][1:len(out[3])+1] for out in outs]
            sparse_bi = [out[2]['2'][1:len(out[3])+1] for out in outs]
            input_ids = [out[3] for out in outs]
            query_vec = np.concatenate([start, end, [[1]]*len(outs)], 1)

            rets = self.mips.search(
                query_vec, (input_ids, sparse_uni, sparse_bi), q_texts=batch_query, nprobe=nprobe,
                doc_top_k=doc_top_k, start_top_k=start_top_k, mid_top_k=mid_top_k, top_k=top_k,
                search_strategy=search_strategy, filter_=args.filter, max_answer_length=max_answer_length,
                sparse_weight=sparse_weight, aggregate=aggregate
            )
            t1 = time()
            out = {'ret': rets, 'time': int(1000 * (t1 - t0))}
            return out


        @app.route('/')
        def index():
            return render_template(f'index.html')

        @app.route('/files/<path:path>')
        def static_files(path):
            return app.send_static_file('files/' + path)

        @app.route('/cached_example', methods=['GET'])
        def cached_example():
            start_time = time()
            q_id = request.args['q_id']
            res, query, query_info = get_cached(search_examples, q_id, query_entity_ids, cached_set)
            latency = time() - start_time
            latency = format(latency, ".3f")
            return render_template(f'cached.html', latency=latency, res=res, query=query, query_info=query_info)

        # This one uses a default hyperparameters
        @app.route('/api', methods=['GET'])
        def api():
            query = request.args['query']
            strat = request.args['strat']
            out = batch_search(
                [query],
                max_answer_length=args.max_answer_length,
                top_k=args.top_k,
                nprobe=args.nprobe,
                search_strategy=strat,
                doc_top_k=args.doc_top_k
            )
            out['ret'] = out['ret'][0]
            return jsonify(out)

        @app.route('/batch_api', methods=['POST'])
        def batch_api():
            batch_query = json.loads(request.form['query'])
            max_answer_length = int(request.form['max_answer_length'])
            start_top_k = int(request.form['start_top_k'])
            mid_top_k = int(request.form['mid_top_k'])
            top_k = int(request.form['top_k'])
            doc_top_k = int(request.form['doc_top_k'])
            nprobe = int(request.form['nprobe'])
            sparse_weight = float(request.form['sparse_weight'])
            strat = request.form['strat']
            out = batch_search(
                batch_query,
                max_answer_length=max_answer_length,
                start_top_k=start_top_k,
                mid_top_k=mid_top_k,
                top_k=top_k,
                doc_top_k=doc_top_k,
                nprobe=nprobe,
                sparse_weight=sparse_weight,
                search_strategy=strat,
                aggregate=args.aggregate
            )
            return jsonify(out)

        @app.route('/get_examples', methods=['GET'])
        def get_examples():
            return render_template(f'example.html', res = index_example_set)

        if self.query_port is None:
            logger.info('You must set self.query_port for querying. You can use self.update_query_port() later on.')
        logger.info(f'Starting Index server at {self.get_address(index_port)}')
        http_server = HTTPServer(WSGIContainer(app))
        http_server.listen(index_port)
        IOLoop.instance().start()

    def serve_doc_ranker(self, doc_port, args):
        doc_ranker_path = os.path.join(args.dump_dir, args.doc_ranker_name)
        doc_ranker = TfidfDocRanker(doc_ranker_path, strict=False)
        app = Flask(__name__)
        app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
        CORS(app)

        @app.route('/doc_index', methods=['POST'])
        def doc_index():
            batch_query = json.loads(request.form['query'])
            doc_idxs = json.loads(request.form['doc_idxs'])
            outs = doc_ranker.batch_doc_scores(batch_query, doc_idxs)
            logger.info(f'Returning {len(outs)} from batch_doc_scores')
            return jsonify(outs)

        @app.route('/top_docs', methods=['POST'])
        def top_docs():
            batch_query = json.loads(request.form['query'])
            top_k = int(request.form['top_k'])
            batch_results = doc_ranker.batch_closest_docs(batch_query, k=top_k)
            top_idxs = [b[0] for b in batch_results]
            top_scores = [b[1].tolist() for b in batch_results]
            logger.info(f'Returning from batch_doc_scores')
            return jsonify([top_idxs, top_scores])

        @app.route('/doc_meta', methods=['POST'])
        def doc_meta():
            pmid = request.form['pmid']
            doc_meta = doc_ranker.get_doc_meta(pmid)
            # logger.info(f'Returning {len(doc_meta)} metadata from get_doc_meta')
            return jsonify(doc_meta)

        @app.route('/text2spvec', methods=['POST'])
        def text2spvec():
            batch_query = json.loads(request.form['query'])
            q_spvecs = [doc_ranker.text2spvec(q, val_idx=True) for q in batch_query]
            q_vals = [q_spvec[0].tolist() for q_spvec in q_spvecs]
            q_idxs = [q_spvec[1].tolist() for q_spvec in q_spvecs]
            logger.info(f'Returning {len(q_vals), len(q_idxs)} q_spvecs')
            return jsonify([q_vals, q_idxs])

        logger.info(f'Starting DocRanker server at {self.get_address(doc_port)}')
        http_server = HTTPServer(WSGIContainer(app))
        http_server.listen(doc_port)
        IOLoop.instance().start()

    def get_address(self, port):
        assert self.base_ip is not None and len(port) > 0
        return self.base_ip + ':' + port

    def embed_query(self, batch_query):
        emb_session = FuturesSession()
        r = emb_session.post(self.get_address(self.query_port) + '/batch_api', data={'query': json.dumps(batch_query)})
        def map_():
            result = r.result()
            emb = result.json()
            return emb, result.elapsed.total_seconds() * 1000
        return map_

    def embed_all_query(self, questions, batch_size=16):
        all_outs = []
        for q_idx in tqdm(range(0, len(questions), batch_size)):
            outs, _ = self.embed_query(questions[q_idx:q_idx+batch_size])()
            all_outs += outs
        start = np.concatenate([out[0] for out in all_outs], 0)
        end = np.concatenate([out[1] for out in all_outs], 0)

        # input ids are truncated (no [CLS], [SEP]) but sparse vals are not ([CLS] max_len [SEP])
        sparse_uni = [out[2]['1'][1:len(out[3])+1] for out in all_outs]
        sparse_bi = [out[2]['2'][1:len(out[3])+1] for out in all_outs]
        input_ids = [out[3] for out in all_outs]
        query_vec = np.concatenate([start, end, [[1]]*len(all_outs)], 1)
        logger.info(f'Query reps: {query_vec.shape}, {len(input_ids)}, {len(sparse_uni)}, {len(sparse_bi)}')
        return query_vec, input_ids, sparse_uni, sparse_bi

    def query(self, query, search_strategy='hybrid'):
        params = {'query': query, 'strat': search_strategy}
        res = requests.get(self.get_address(self.index_port) + '/api', params=params)
        if res.status_code != 200:
            logger.info('Wrong behavior %d' % res.status_code)
        try:
            outs = json.loads(res.text)
        except Exception as e:
            logger.info(f'no response or error for q {query}')
            logger.info(res.text)
        return outs

    def batch_query(self, batch_query, max_answer_length=20, start_top_k=1000, mid_top_k=100, top_k=10, doc_top_k=5,
                    nprobe=64, sparse_weight=0.05, search_strategy='hybrid'):
        post_data = {
            'query': json.dumps(batch_query),
            'max_answer_length': max_answer_length,
            'start_top_k': start_top_k,
            'mid_top_k': mid_top_k,
            'top_k': top_k,
            'doc_top_k': doc_top_k,
            'nprobe': nprobe,
            'sparse_weight': sparse_weight,
            'strat': search_strategy,
        }
        res = requests.post(self.get_address(self.index_port) + '/batch_api', data=post_data)
        if res.status_code != 200:
            logger.info('Wrong behavior %d' % res.status_code)
        try:
            outs = json.loads(res.text)
        except Exception as e:
            logger.info(f'no response or error for q {batch_query}')
            logger.info(res.text)
        return outs

    def get_doc_scores(self, batch_query, doc_idxs):
        post_data = {
            'query': json.dumps(batch_query),
            'doc_idxs': json.dumps(doc_idxs)
        }
        res = requests.post(self.get_address(self.doc_port) + '/doc_index', data=post_data)
        if res.status_code != 200:
            logger.info('Wrong behavior %d' % res.status_code)
        try:
            result = json.loads(res.text)
        except Exception as e:
            logger.info(f'no response or error for {doc_idxs}')
            logger.info(res.text)
        return result

    def get_top_docs(self, batch_query, top_k):
        post_data = {
            'query': json.dumps(batch_query),
            'top_k': top_k
        }
        res = requests.post(self.get_address(self.doc_port) + '/top_docs', data=post_data)
        if res.status_code != 200:
            logger.info('Wrong behavior %d' % res.status_code)
        try:
            result = json.loads(res.text)
        except Exception as e:
            logger.info(f'no response or error for {top_k}')
            logger.info(res.text)
        return result

    def get_doc_meta(self, pmid):
        post_data = {
            'pmid': pmid
        }
        res = requests.post(self.get_address(self.doc_port) + '/doc_meta', data=post_data)
        if res.status_code != 200:
            logger.info('Wrong behavior %d' % res.status_code)
        try:
            result = json.loads(res.text)
        except Exception as e:
            logger.info(f'no response or error for {pmid}')
            logger.info(res.text)
        return result

    def get_q_spvecs(self, batch_query):
        post_data = {'query': json.dumps(batch_query)}
        res = requests.post(self.get_address(self.doc_port) + '/text2spvec', data=post_data)
        if res.status_code != 200:
            logger.info('Wrong behavior %d' % res.status_code)
        try:
            result = json.loads(res.text)
        except Exception as e:
            logger.info(f'no response or error for q {batch_query}')
            logger.info(res.text)
        return result



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # QueryEncoder
    parser.add_argument('--metadata_dir', default='/nvme/jinhyuk/denspi/bert', type=str)
    parser.add_argument("--vocab_name", default='vocab.txt', type=str)
    parser.add_argument("--bert_config_name", default='bert_config.json', type=str)
    parser.add_argument("--bert_model_option", default='large_uncased', type=str)
    parser.add_argument("--parallel", default=False, action='store_true')
    parser.add_argument("--do_case", default=False, action='store_true')
    parser.add_argument("--use_biobert", default=False, action='store_true')
    parser.add_argument("--query_encoder_path", default='/nvme/jinhyuk/denspi/KR94373_piqa-nfs_1173/1/model.pt', type=str)
    parser.add_argument("--query_port", default='-1', type=str)

    # DocRanker
    parser.add_argument('--doc_ranker_name', default='docs-tfidf-ngram=2-hash=16777216-tokenizer=simple.npz')
    parser.add_argument('--doc_port', default='-1', type=str)

    # PhraseIndex
    parser.add_argument('--dump_dir', default='/nvme/jinhyuk/denspi/1173_wikipedia_filtered')
    parser.add_argument('--phrase_dir', default='phrase')
    parser.add_argument('--tfidf_dir', default='tfidf')
    parser.add_argument('--index_dir', default='1048576_hnsw_SQ8')
    parser.add_argument('--index_name', default='index.faiss')
    parser.add_argument('--idx2id_name', default='idx2id.hdf5')
    parser.add_argument('--index_port', default='-1', type=str)
    parser.add_argument('--sent_search', default=False, action='store_true')

    # These can be dynamically changed.
    parser.add_argument('--max_answer_length', default=20, type=int)
    parser.add_argument('--start_top_k', default=1000, type=int)
    parser.add_argument('--mid_top_k', default=100, type=int)
    parser.add_argument('--top_k', default=10, type=int)
    parser.add_argument('--doc_top_k', default=5, type=int)
    parser.add_argument('--nprobe', default=256, type=int)
    parser.add_argument('--sparse_weight', default=0.05, type=float)
    parser.add_argument('--search_strategy', default='hybrid')
    parser.add_argument('--filter', default=False, action='store_true')
    parser.add_argument('--aggregate', default=False, action='store_true')
    parser.add_argument('--no_para', default=False, action='store_true')

    # Serving options
    parser.add_argument('--examples_path', default='queries/examples_covid_paraphrased.json')
    parser.add_argument('--google_examples_path', default='queries/examples_google_query.json')
    parser.add_argument('--kcw_examples_path', default='queries/examples_covid_eval_kcw.json')
    parser.add_argument('--top100_covid_examples_path', default='queries/top100_covid_paraphrased.json')
    parser.add_argument('--top100_google_examples_path', default='queries/top100_google_query.json')
    parser.add_argument('--top10_kcw_examples_path', default='queries/top10_covid_eval_kcw.json')
    parser.add_argument('--develop', default=False, action='store_true')

    # Training (query_encoder)
    parser.add_argument('--train_path', default=None)
    parser.add_argument('--train_batch_size', default=10, type=int)
    parser.add_argument('--num_train_epochs', default=10, type=int)
    parser.add_argument("--learning_rate", default=3e-5, type=float)
    parser.add_argument("--warmup_proportion", default=0.1, type=float)
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1)
    parser.add_argument('--hard_em', default=False, action='store_true')
    parser.add_argument('--model_save_dir', default='models')

    # Evaluation
    parser.add_argument('--test_path', default='datasets/open-qa/squad/dev-v1.1_preprocessed_sampled.json')
    parser.add_argument('--candidate_path', default=None)
    parser.add_argument('--regex', default=False, action='store_true')
    parser.add_argument('--eval_batch_size', default=10, type=int)
    parser.add_argument('--top_phrase_path', default='top_phrases.json')

    # Run mode
    parser.add_argument('--base_ip', default='http://163.152.163.248')
    parser.add_argument('--run_mode', default='batch_query')
    parser.add_argument('--cuda', default=False, action='store_true')
    parser.add_argument('--draft', default=False, action='store_true')
    parser.add_argument('--seed', default=1992, type=int)
    args = parser.parse_args()

    # Seed for reproducibility
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    natural_kb = NaturalKB(args)

    # Set ports
    # natural_kb.query_port = '9010'
    # natural_kb.doc_port = '9020'
    # natural_kb.index_port = '10001'

    if args.run_mode == 'q_serve':
        logger.info(f'Query address: {natural_kb.get_address(natural_kb.query_port)}')
        natural_kb.serve_query_encoder(args.query_port, args)

    elif args.run_mode == 'd_serve':
        logger.info(f'Doc address: {natural_kb.get_address(natural_kb.doc_port)}')
        natural_kb.serve_doc_ranker(args.doc_port, args)

    elif args.run_mode == 'p_serve':
        logger.info(f'Query address: {natural_kb.get_address(natural_kb.query_port)}')
        logger.info(f'Doc address: {natural_kb.get_address(natural_kb.doc_port)}')
        logger.info(f'Index address: {natural_kb.get_address(natural_kb.index_port)}')
        natural_kb.serve_phrase_index(args.index_port, args)

    elif args.run_mode == 'query':
        logger.info(f'Index address: {natural_kb.get_address(natural_kb.index_port)}')
        query = 'Which Lisp framework has been developed for image processing?'
        # query = ' Several genetic factors have been related to HIV-1 resistance'
        result = natural_kb.query(query)
        logger.info(f'Answers to a question: {query}')
        logger.info(f'{[r["answer"] for r in result["ret"]]}')

        # Reshape metadata
        for result_ in result['ret']:
            meta = result_['metadata']
            if 'context_entities' not in meta:
                meta['context_entities'] = {}
                continue
            context_ents = [ # Initialize
                {k: [v_ for v_ in v.values()] for k, v in val.items()} for val in meta['context_entities'].values()
            ][0]
            assert len(meta['context_entities']) == 1, 'Currently single para is supported'
            for ii in range(1, len(meta['context_entities'])):
                for k, v in meta['context_entities'][ii]:
                    context_ents[k] += v
            meta['context_entities'] = context_ents

    elif args.run_mode == 'batch_query':
        logger.info(f'Index address: {natural_kb.get_address(natural_kb.index_port)}')
        queries = [
            'Which Lisp framework has been developed for image processing?',
            'What are the 3 main bacteria found in human milk?',
            'Where did COVID-19 happen?'
        ]
        result = natural_kb.batch_query(
            queries,
            max_answer_length=args.max_answer_length,
            start_top_k=args.start_top_k,
            mid_top_k=args.mid_top_k,
            top_k=args.top_k,
            doc_top_k=args.doc_top_k,
            nprobe=args.nprobe,
            sparse_weight=args.sparse_weight,
            search_strategy=args.search_strategy,
        )
        for query, result in zip(queries, result['ret']):
            logger.info(f'Answers to a question: {query}')
            logger.info(f'{[r["answer"] for r in result]}')

    else:
        raise NotImplementedError
