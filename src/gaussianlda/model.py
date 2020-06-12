import json
import math
import os
import pickle
import re
import warnings

import numpy as np
from gaussianlda.prior import Wishart
from scipy.linalg import solve_triangular
from scipy.special import gammaln
from sklearn.metrics.pairwise import cosine_similarity


class GaussianLDA:
    """
    Trained model.

    First train using the GaussianLDATrainer or GaussianLDAAliasTrainer.
    Then load using this class to get a GaussianLDA with the saved parameters for performing
    inference on new data without updating the parameters.

    """
    def __init__(self, vocab_embeddings, vocab, num_tables, alpha, kappa, table_counts, table_means, log_determinants,
                 table_cholesky_ltriangular_mat):
        # Vocab is used for outputting topics
        self.vocab = vocab

        # Dirichlet hyperparam
        self.alpha = alpha

        # dataVectors
        self.vocab_embeddings = vocab_embeddings
        self.embedding_size = vocab_embeddings.shape[1]
        # numIterations
        # K, num tables
        self.num_tables = num_tables
        # Number of customers observed at each table
        self.table_counts = table_counts
        # Mean vector associated with each table
        # This is the bayesian mean (i.e has the prior part too)
        self.table_means = table_means
        # log-determinant of covariance matrix for each table.
        # Since 0.5 * logDet is required in (see logMultivariateTDensity), that value is kept.
        self.log_determinants = log_determinants
        # Cholesky Lower Triangular Decomposition of covariance matrix associated with each table.
        self.table_cholesky_ltriangular_mat = table_cholesky_ltriangular_mat

        # Normal inverse wishart prior
        self.prior = Wishart(self.vocab_embeddings, kappa=kappa)

        # Cache k_0\mu_0\mu_0^T, only compute it once
        # Used in calculate_table_params()
        self.k0mu0mu0T = self.prior.kappa * np.outer(self.prior.mu, self.prior.mu)

        # Since we ignore the document's contributions to the global parameters when sampling,
        # we can precompute a whole load of parts of the likelihood calculation.
        # Table counts are not updated for the document in question, as it's assumed to make
        # a tiny contribution compared to the whole training corpus.
        k_n = self.prior.kappa + self.table_counts
        nu_n = self.prior.nu + self.table_counts
        self.scaleTdistrn = np.sqrt((k_n + 1.) / (k_n * (nu_n - self.embedding_size + 1.)))
        self.nu = self.prior.nu + self.table_counts - self.embedding_size + 1.

    @staticmethod
    def load_from_java(path, vocab_embeddings_path, vocab_path, alpha=None, kappa=None, iteration=-1,
                       output_checks=False):
        """
        NB: This isn't fully working yet!

        Load a Gaussian LDA model trained and saved by the Gaussian LDA authors' original
        Java code, available at https://github.com/rajarshd/Gaussian_LDA/.
        The path given is to the directory containing all of the model files.

        Embeddings and vocab are not stored with the model, so need to be provided.
        They are given as paths to the files, stored in the same format expected by Gaussian
        LDA. This means you can use exactly the files you used to train the model.

        The files contain the output from each iteration of training. We just get the last iteration,
        or another one if explicitly requested.

        Unfortunately, the output does not store the hyperparameters alpha and kappa.
        kappa is needed to compute likelihoods under the topics and alpha is needed to perform
        topic inference, so you need to make sure these are correct to do correct inference.
        If not given, the default values from training are used.

        """
        # Load the vocab
        with open(vocab_path, "r") as f:
            vocab = [w.rstrip("\n") for w in f.readlines()]
        # Load the embeddings
        with open(vocab_embeddings_path, "r") as f:
            # Read the first line to check the dimensionality
            embedding_size = len(f.readline().split())
            # Put the embeddings into a Numpy array
            vocab_embeddings = np.zeros((len(vocab), embedding_size), dtype=np.float64)
            # We don't go back and read the first line, as it's never used by the model
            for i, line in enumerate(f):
                line = line.rstrip("\n")
                vocab_embeddings[i, :] = [float(v) for v in line.split()]

        if output_checks:
            # Sanity check the embeddings and vocab
            for w, top_word in enumerate(vocab[:3]):
                nns = np.argsort(-cosine_similarity(vocab_embeddings[w].reshape(1,-1), vocab_embeddings)[0])
                print("NNs to {}: {}".format(top_word, ", ".join(vocab[nb] for nb in nns[:5])))

        filenames = os.listdir(path)
        table_params_re = re.compile("\d+\.txt")
        table_params_filenames = [f for f in filenames if re.match(table_params_re, f)]

        num_tables = len(table_params_filenames)
        num_iterations = None

        # Create empty arrays to fill
        table_cholesky_ltriangular_mat = np.zeros((num_tables, embedding_size, embedding_size), dtype=np.float64)
        table_means = np.zeros((num_tables, embedding_size), dtype=np.float64)
        for table_filename in table_params_filenames:
            # Filename is of the form k.txt
            table_num = int(table_filename[:-4])
            with open(os.path.join(path, table_filename), "r") as f:
                lines = f.readlines()
            # Each iteration stores D+1 lines: 1 line of the mean and D lines of the matrix
            file_num_iterations = len(lines) // (embedding_size+1)
            if len(lines) % (embedding_size+1) != 0:
                warnings.warn("Gaussian LDA model does not have an exact number of iterations stored "
                              "({} iterations, plus {} lines)".format(file_num_iterations,
                                                                      len(lines) % (embedding_size+1)))
            if num_iterations is not None and file_num_iterations != num_iterations:
                warnings.warn("Different numbers of iterations stored for different tables: {} != {}"
                              .format(file_num_iterations, num_iterations))
            num_iterations = file_num_iterations
            if iteration == -1:
                iteration_start_line = (num_iterations-1)*(embedding_size+1)
            else:
                iteration_start_line = iteration * (embedding_size+1)
            # The last D lines are the matrix and the one before is the mean
            # The first line contains the table mean
            table_mean = [float(v) for v in lines[iteration_start_line].split()]
            if len(table_mean) != embedding_size:
                raise ValueError("expected {}-size mean, but got {} for table {}".format(
                    embedding_size, len(table_mean), table_num))
            table_means[table_num, :] = table_mean
            # The remaining lines are the chol decomp of the cov matrix
            chol_mat = np.array([
                [float(v) for v in line.split()]
                for line in lines[iteration_start_line+1:iteration_start_line+embedding_size+1]
            ], dtype=np.float64)
            table_cholesky_ltriangular_mat[table_num, :, :] = chol_mat

        # Compute the log determinants from the chol decomposition of the covariance matrices
        log_determinants = np.zeros(num_tables, dtype=np.float64)
        for table in range(num_tables):
            # Log det of cov matrix is 2*log det of chol decomp
            log_determinants[table] = 2. * np.linalg.slogdet(table_cholesky_ltriangular_mat[table])[1]

        with open(os.path.join(path, "topic_counts.txt"), "r") as f:
            lines = f.readlines()
        if iteration == -1:
            # The last K lines give us the final counts
            table_counts = np.array([float(v) for v in lines[-num_tables:]], dtype=np.float64)
        else:
            table_counts = np.array(
                [float(v) for v in lines[iteration*num_tables:(iteration+1)*num_tables]], dtype=np.float64)

        if alpha is None:
            alpha = 1. / num_tables
        if kappa is None:
            kappa = 0.1

        # Initialize a model
        model = GaussianLDA(
            vocab_embeddings, vocab, num_tables, alpha, kappa,
            table_counts, table_means, log_determinants, table_cholesky_ltriangular_mat,
        )
        return model

    @staticmethod
    def load(path):
        # Load JSON hyperparams
        with open(os.path.join(path, "params.json"), "r") as f:
            hyperparams = json.load(f)

        # Load numpy arrays for model parameters
        arrs = {}
        for name in [
            "table_counts", "table_means", "log_determinants", "table_cholesky_ltriangular_mat", "vocab_embeddings",
        ]:
            with open(os.path.join(path, "{}.pkl".format(name)), "rb") as f:
                arrs[name] = pickle.load(f)

        # Initialize a model
        model = GaussianLDA(
            arrs["vocab_embeddings"], hyperparams["vocab"], hyperparams["num_tables"], hyperparams["alpha"],
            hyperparams["kappa"],
            arrs["table_counts"], arrs["table_means"], arrs["log_determinants"],
            arrs["table_cholesky_ltriangular_mat"],
        )
        return model

    def sample(self, doc, num_iterations):
        """
        Run Gibbs sampler on a single document without updating global parameters.

        for num_iters:
            for each customer
                remove him from his old_table and update the table params.
                if old_table is empty:
                    remove table
                Calculate prior and likelihood for this customer sitting at each table
                sample for a table index
        """
        table_assignments = list(np.random.randint(self.num_tables, size=len(doc)))
        doc_table_counts = np.bincount(table_assignments, minlength=self.num_tables)

        for iteration in range(num_iterations):
            for w, cust_id in enumerate(doc):
                x = self.vocab_embeddings[cust_id]

                # Remove custId from his old_table
                old_table_id = table_assignments[w]
                table_assignments[w] = -1  # Doesn't really make any difference, as only counts are used
                doc_table_counts[old_table_id] -= 1

                # Now calculate the prior and likelihood for the customer to sit in each table and sample
                # Go over each table
                counts = doc_table_counts[:] + self.alpha
                # Now calculate the likelihood for each table
                log_lls = self.log_multivariate_tdensity_tables(x)
                # Add log prior in the posterior vector
                log_posteriors = np.log(counts) + log_lls
                # To prevent overflow, subtract by log(p_max).
                # This is because when we will be normalizing after exponentiating,
                # each entry will be exp(log p_i - log p_max )/\Sigma_i exp(log p_i - log p_max)
                # the log p_max cancels put and prevents overflow in the exponentiating phase.
                posterior = np.exp(log_posteriors - log_posteriors.max())
                posterior /= posterior.sum()
                # Now sample an index from this posterior vector.
                new_table_id = np.random.choice(self.num_tables, p=posterior)

                # Now have a new assignment: add its counts
                doc_table_counts[new_table_id] += 1
                table_assignments[w] = new_table_id
        return table_assignments

    def log_multivariate_tdensity(self, x, table_id):
        """
        Gaussian likelihood for a table-embedding pair when using Cholesky decomposition.

        """
        if x.ndim > 1:
            logprobs = np.zeros(x.shape[0], dtype=np.float64)
            for i in range(x.shape[0]):
                logprobs[i] = self.log_multivariate_tdensity(x[i], table_id)
            return logprobs

        count = self.table_counts[table_id]
        k_n = self.prior.kappa + count
        nu_n = self.prior.nu + count
        scaleTdistrn = np.sqrt((k_n + 1.) / (k_n * (nu_n - self.embedding_size + 1.)))
        nu = self.prior.nu + count - self.embedding_size + 1.
        # Since I am storing lower triangular matrices, it is easy to calculate (x-\mu)^T\Sigma^-1(x-\mu)
        # therefore I am gonna use triangular solver
        # first calculate (x-mu)
        x_minus_mu = x - self.table_means[table_id]
        # Now scale the lower tringular matrix
        ltriangular_chol = scaleTdistrn * self.table_cholesky_ltriangular_mat[table_id]
        solved = solve_triangular(ltriangular_chol, x_minus_mu, check_finite=False)
        # Now take xTx (dot product)
        val = (solved ** 2.).sum(-1)

        logprob = gammaln((nu + self.embedding_size) / 2.) - \
                  (
                          gammaln(nu / 2.) +
                          self.embedding_size / 2. * (np.log(nu) + np.log(math.pi)) +
                          self.log_determinants[table_id] +
                          (nu + self.embedding_size) / 2. * np.log(1. + val / nu)
                  )
        return logprob

    def log_multivariate_tdensity_tables(self, x):
        """
        Gaussian likelihood for a table-embedding pair when using Cholesky decomposition.
        This version computes the likelihood for all tables in parallel.

        """
        # Since I am storing lower triangular matrices, it is easy to calculate (x-\mu)^T\Sigma^-1(x-\mu)
        # therefore I am gonna use triangular solver first calculate (x-mu)
        x_minus_mu = x[None, :] - self.table_means
        # Now scale the lower tringular matrix
        ltriangular_chol = self.scaleTdistrn[:, None, None] * self.table_cholesky_ltriangular_mat
        # We can't do solve_triangular for all matrices at once in scipy
        val = np.zeros(self.num_tables, dtype=np.float64)
        for table in range(self.num_tables):
            table_solved = solve_triangular(ltriangular_chol[table], x_minus_mu[table])
            # Now take xTx (dot product)
            val[table] = (table_solved ** 2.).sum()

        logprob = gammaln((self.nu + self.embedding_size) / 2.) - \
                  (
                          gammaln(self.nu / 2.) +
                          self.embedding_size / 2. * (np.log(self.nu) + np.log(math.pi)) +
                          self.log_determinants +
                          (self.nu + self.embedding_size) / 2. * np.log(1. + val / self.nu)
                  )
        return logprob
